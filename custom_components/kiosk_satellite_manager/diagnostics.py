"""KSM-BEHAVE-161 (#133): Home Assistant's "Download diagnostics" for a KSM
config entry -- the support record (support_log.py) plus an allowlisted view
of the entry. Entry data and options are never copied wholesale."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import fleet, support_log, support_request
from .const import (
    ACME_DEVICE_FIELDS, CONF_ACME_ACCOUNT_KEY, CONF_ACME_ACCOUNT_URL, CONF_ACME_DNS_TOKEN,
    CONF_ACME_EMAIL, CONF_DEVICE_PROFILE, CONF_ENTRY_TYPE, CONF_HA_REFRESH_TOKEN_ID, CONF_HA_TOKEN, CONF_HA_URL,
    CONF_HOST, CONF_KEY_PATH, CONF_NAME, CONF_PASSWORD, CONF_TLS_SPKI, DOMAIN,
    ENTRY_TYPE_FLEET, ENTRY_TYPE_MANAGER, ENTRY_TYPE_UNMANAGED,
)
from .meta_setup import PENDING_KEY

# Second layer only: nothing below copies these keys in the first place.
TO_REDACT = {
    CONF_PASSWORD, CONF_HA_TOKEN, CONF_HA_REFRESH_TOKEN_ID, CONF_TLS_SPKI, CONF_KEY_PATH,
    CONF_HOST, CONF_NAME, CONF_HA_URL, "token", "access_token", "refresh_token", "pin",
    "serial", "mac", "title", "unique_id",
    # KSM-BEHAVE-203 (#200): Certificates secrets and every device certificate field.
    CONF_ACME_DNS_TOKEN, CONF_ACME_ACCOUNT_KEY, CONF_ACME_ACCOUNT_URL, CONF_ACME_EMAIL,
    *ACME_DEVICE_FIELDS,
}
_ENTRY_TYPES = {ENTRY_TYPE_MANAGER, ENTRY_TYPE_UNMANAGED, ENTRY_TYPE_FLEET}


def _devices(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    entry_type = entry.data.get(CONF_ENTRY_TYPE)
    if entry_type in (ENTRY_TYPE_UNMANAGED, ENTRY_TYPE_FLEET):
        devices = fleet.device_entries(hass, entry)
    elif not entry_type:
        devices = [entry]
    else:
        devices = []
    return [
        {
            "model": support_log.model_label(device.data.get(CONF_DEVICE_PROFILE)),
            "meta_watch_pending": isinstance(device.data.get(PENDING_KEY), dict),
            # KSM-BEHAVE-166: carries the whole request when the URL could not.
            "support_request": support_request.remembered(hass, device.entry_id),
        }
        for device in devices
    ]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    entry_type = entry.data.get(CONF_ENTRY_TYPE)
    integration = await async_get_integration(hass, DOMAIN)
    await support_log.async_load(hass)
    return async_redact_data(
        {
            "version": str(integration.version),
            "entry_type": entry_type if entry_type in _ENTRY_TYPES else "device",
            "devices": _devices(hass, entry),
            "support_log": support_log.runs(hass),
        },
        TO_REDACT,
    )


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device
) -> dict[str, Any]:
    """Download the selected KSM device's allowlisted support data (#193)."""
    selected = None
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        candidate = fleet.resolve_device(hass, identifier)
        if candidate is None:
            continue
        owner = getattr(candidate, "parent", candidate)
        if owner.entry_id == entry.entry_id:
            selected = candidate
            break
    if selected is None:
        raise ValueError("HA device does not belong to this KSM entry")
    integration = await async_get_integration(hass, DOMAIN)
    return async_redact_data(
        {"version": str(integration.version), "devices": [{
            "model": support_log.model_label(selected.data.get(CONF_DEVICE_PROFILE)),
            "meta_watch_pending": isinstance(selected.data.get(PENDING_KEY), dict),
            "support_request": support_request.remembered(hass, selected.entry_id),
        }]},
        TO_REDACT,
    )
