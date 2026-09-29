"""KSM-BEHAVE-135 (#96): the wizard adds the kiosk to HA's ESPHome integration.

Turns Kiosk Satellite's ESPHome server on, waits for it to generate the
Encryption key shown on its ESPHome page (`btproxy.key`), then drives HA's own
ESPHome discovery flow for this node with that key. KSM never edits an existing
ESPHome entry. The key is never logged.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import ks_api_client
from .esphome_identity import ENABLED_SETTING, NODE_NAME_SETTING, _node_name_unset
from .ks_api_client import KsApiError
from .rename import ESPHOME_DOMAIN, _host_ip, derive_rename_names

_LOGGER = logging.getLogger(__name__)

ADDED = "added"
ALREADY_ADDED = "already_added"
KEY_TIMEOUT = "key_timeout"
DISCOVERY_TIMEOUT = "discovery_timeout"
FAILED = "failed"

KEY_SETTING = "btproxy.key"
KEY_TIMEOUT_S = 30.0
DISCOVERY_TIMEOUT_S = 90.0
LOAD_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 2.0

_NOISE_PSK = "noise_psk"
_KEY_STEPS = frozenset({"encryption_key", "reauth_confirm"})
_MAX_FLOW_STEPS = 6


async def _poll(check: Callable[[], Awaitable], timeout: float):
    """The first truthy result of `check`, or None once `timeout` passes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = await check()
        if result:
            return result
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(POLL_INTERVAL_S)


async def _entries_at(hass: HomeAssistant, host: str) -> list:
    kiosk_ip = await _host_ip(hass, host)
    if kiosk_ip is None:
        return []
    return [
        entry
        for entry in hass.config_entries.async_entries(ESPHOME_DOMAIN)
        if entry.disabled_by is None
        and entry.data.get("host")
        and await _host_ip(hass, entry.data["host"]) == kiosk_ip
    ]


async def _drive_flow(hass: HomeAssistant, flow: dict, key: str) -> str:
    flow_id, step = flow["flow_id"], flow.get("step_id")
    for _ in range(_MAX_FLOW_STEPS):
        user_input = {_NOISE_PSK: key} if step in _KEY_STEPS else {}
        result = await hass.config_entries.flow.async_configure(flow_id, user_input)
        kind = result["type"]
        if kind == FlowResultType.CREATE_ENTRY:
            return ADDED
        if kind == FlowResultType.ABORT:
            return ALREADY_ADDED if result.get("reason") == "already_configured" else FAILED
        if kind != FlowResultType.FORM or result.get("errors"):
            return FAILED
        step = result["step_id"]
    return FAILED


async def async_adopt(
    hass: HomeAssistant, *, host: str, name: str, password: str, pin: str | None
) -> str:
    """One of ADDED, ALREADY_ADDED, KEY_TIMEOUT, DISCOVERY_TIMEOUT, FAILED."""
    session = async_get_clientsession(hass)
    try:
        token = await ks_api_client.login(session, host, password, pin=pin)
        current = await ks_api_client.get_settings(session, host, token, pin=pin)
        node_name = current.get(NODE_NAME_SETTING)
        payload: dict = {}
        if _node_name_unset(node_name):
            node_name = derive_rename_names(name).esphome_node_name
            payload[NODE_NAME_SETTING] = node_name
        if not current.get(ENABLED_SETTING):
            payload[ENABLED_SETTING] = True
        if payload:
            await ks_api_client.patch_settings(session, host, token, payload, pin=pin)

        if await _entries_at(hass, host):
            return ALREADY_ADDED

        async def read_key() -> str:
            settings = await ks_api_client.get_settings(session, host, token, pin=pin)
            return settings.get(KEY_SETTING) or ""

        key = await _poll(read_key, KEY_TIMEOUT_S)
        if not key:
            return KEY_TIMEOUT

        async def find_flow() -> dict | None:
            for flow in hass.config_entries.flow.async_progress_by_handler(ESPHOME_DOMAIN):
                shown = (flow.get("context", {}).get("title_placeholders") or {}).get("name")
                if shown and shown.lower() == node_name.lower():
                    return flow
            return None

        flow = await _poll(find_flow, DISCOVERY_TIMEOUT_S)
        if flow is None:
            return DISCOVERY_TIMEOUT

        outcome = await _drive_flow(hass, flow, key)
        if outcome != ADDED:
            return outcome

        async def loaded() -> bool:
            return any(
                entry.state is ConfigEntryState.LOADED for entry in await _entries_at(hass, host)
            )

        return ADDED if await _poll(loaded, LOAD_TIMEOUT_S) else FAILED
    except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.warning("Could not add %s to ESPHome: %s", name, type(err).__name__)
        return FAILED
