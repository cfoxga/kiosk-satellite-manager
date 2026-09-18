"""Kiosk Satellite Manager integration setup.

Phase 1 lands the real setup: a DataUpdateCoordinator polling the device's
/api/health (shared by the version sensor, and refreshed on demand by the
install button), forwarded to the button/sensor platforms.

Phase 2 adds the `kiosk_satellite_manager.provision` service: apply a
settings payload via the device's `ks.provision` ADB intent and fail loudly
if the /api/health read-back doesn't confirm it landed (KSM-BEHAVE-002).
"""
from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .adb_client import AdbClient
from .const import CONF_HOST, CONF_KEY_PATH, CONF_PORT, DOMAIN, HEALTH_SCAN_INTERVAL_MIN, PLATFORMS
from .provisioning import ProvisioningMismatch, apply_provisioning, fetch_health

_LOGGER = logging.getLogger(__name__)

__all__ = ["DOMAIN", "async_setup_entry", "async_unload_entry"]

SERVICE_PROVISION = "provision"

PROVISION_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): str,
        vol.Required("settings"): dict,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry: start the health-poll coordinator, then the
    button/sensor platforms."""
    session = async_get_clientsession(hass)
    host = entry.data[CONF_HOST]

    async def _update():
        return await fetch_health(session, host)

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_{host}",
        update_method=_update,
        update_interval=timedelta(minutes=HEALTH_SCAN_INTERVAL_MIN),
    )
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_PROVISION):

        async def _handle_provision(call: ServiceCall) -> None:
            target_entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
            if target_entry is None or target_entry.domain != DOMAIN:
                raise ServiceValidationError(
                    f"Unknown {DOMAIN} config entry: {call.data['config_entry_id']}"
                )
            target_coordinator = hass.data.get(DOMAIN, {}).get(target_entry.entry_id)
            client = AdbClient(
                target_entry.data[CONF_HOST],
                target_entry.data[CONF_PORT],
                target_entry.data[CONF_KEY_PATH],
            )
            await client.connect()
            try:
                await apply_provisioning(
                    client,
                    async_get_clientsession(hass),
                    target_entry.data[CONF_HOST],
                    call.data["settings"],
                )
            except ProvisioningMismatch as err:
                raise ServiceValidationError(str(err)) from err
            finally:
                await client.close()
            if target_coordinator is not None:
                await target_coordinator.async_request_refresh()

        hass.services.async_register(
            DOMAIN, SERVICE_PROVISION, _handle_provision, schema=PROVISION_SCHEMA
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, SERVICE_PROVISION)
    return unloaded
