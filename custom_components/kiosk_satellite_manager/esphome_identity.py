"""KSM-BEHAVE-110 (#67): each managed kiosk's ESPHome identity.

A kiosk KSM adopts keeps Kiosk Satellite's defaults, which leave
`esphome.node_name` empty. On each device setup KSM fills an empty node name
with the KSM-BEHAVE-084 slug of the device's name; a name the device already
has is never overwritten. ESPHome itself is turned on only for a device added
while the manager's "ESPHome on new devices" option was on, and only once --
after that the operator's choice on the device stands. Failures are logged
and never fail the entry.
"""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import ks_api_client
from .const import (
    CONF_ESPHOME_ENABLE_PENDING,
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_TLS_SPKI,
)
from .ks_api_client import KsApiError
from .rename import derive_rename_names

_LOGGER = logging.getLogger(__name__)

NODE_NAME_SETTING = "esphome.node_name"
ENABLED_SETTING = "esphome.enabled"


async def async_ensure_esphome_identity(hass: HomeAssistant, entry: ConfigEntry) -> None:
    password = entry.data.get(CONF_PASSWORD)
    if not password:
        return
    name = entry.data.get(CONF_NAME) or entry.title
    pending = bool(entry.data.get(CONF_ESPHOME_ENABLE_PENDING))
    host = entry.data[CONF_HOST]
    pin = entry.data.get(CONF_TLS_SPKI)
    session = async_get_clientsession(hass)
    try:
        node_name = derive_rename_names(name).esphome_node_name
        token = await ks_api_client.login(session, host, password, pin=pin)
        current = await ks_api_client.get_settings(session, host, token, pin=pin)
        payload: dict = {}
        if not current.get(NODE_NAME_SETTING):
            payload[NODE_NAME_SETTING] = node_name
        if pending and not current.get(ENABLED_SETTING):
            payload[ENABLED_SETTING] = True
        if payload:
            await ks_api_client.patch_settings(session, host, token, payload, pin=pin)
    except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.warning("Could not set the ESPHome identity of %s: %s", name, err)
        return
    if pending:
        data = {k: v for k, v in entry.data.items() if k != CONF_ESPHOME_ENABLE_PENDING}
        hass.config_entries.async_update_entry(entry, data=data)
    if payload:
        _LOGGER.info("Kiosk Satellite %s ESPHome settings applied: %s", name, sorted(payload))
