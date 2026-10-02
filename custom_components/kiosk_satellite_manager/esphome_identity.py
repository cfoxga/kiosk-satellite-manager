"""KSM-BEHAVE-110 (#67): each managed kiosk's ESPHome identity.

A kiosk KSM adopts keeps Kiosk Satellite's defaults, which leave
`esphome.node_name` empty (or, once ESPHome runs, KS's generated
`kiosk-satellite-<6 hex>`). On each device setup KSM replaces either with the
KSM-BEHAVE-084 slug of the device's name; any other name is never overwritten. ESPHome itself is turned on only for a device added
while the manager's "ESPHome on new devices" option was on, and only once --
after that the operator's choice on the device stands. Failures are logged
and never fail the entry.
"""
from __future__ import annotations

import logging
import re

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
# KS publishes entities over its ESPHome server only when this is on too (#116).
ENTITIES_SETTING = "esphome.entities"
# Once its ESPHome server runs, Kiosk Satellite fills a blank node name with
# this generated default (seen live on dev, KS 2026.9.88); nobody chose it.
_KS_GENERATED_NODE_NAME = re.compile(r"kiosk-satellite-[0-9a-f]{6}")


def _node_name_unset(value: str | None) -> bool:
    return not value or bool(_KS_GENERATED_NODE_NAME.fullmatch(value))


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
        if _node_name_unset(current.get(NODE_NAME_SETTING)):
            payload[NODE_NAME_SETTING] = node_name
        if pending:
            for setting in (ENABLED_SETTING, ENTITIES_SETTING):
                if not current.get(setting):
                    payload[setting] = True
        if payload:
            await ks_api_client.patch_settings(session, host, token, payload, pin=pin)
    except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.warning("Could not set the ESPHome identity of %s: %s", name, err)
        return
    if pending:
        data = {k: v for k, v in entry.data.items() if k != CONF_ESPHOME_ENABLE_PENDING}
        from . import fleet
        fleet.update_device(hass, entry, data=data)
    if payload:
        _LOGGER.info("Kiosk Satellite %s ESPHome settings applied: %s", name, sorted(payload))
