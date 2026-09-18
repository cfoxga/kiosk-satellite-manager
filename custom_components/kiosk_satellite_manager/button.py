"""Install/Reinstall Kiosk Satellite button entity (Phase 1).

Fetches the KS release matching the device's detected ABI, pushes and
installs it, then asks the version sensor's coordinator to refresh so the
new version shows up without waiting for the next poll.
"""
from __future__ import annotations

import logging
import os
import tempfile

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .adb_client import AdbClient
from .const import CONF_HOST, CONF_KEY_PATH, CONF_PORT, DOMAIN, KS_APK_REMOTE_PATH
from .ks_api import latest_apk_url

_LOGGER = logging.getLogger(__name__)


def _write_temp_apk(data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".apk")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([KioskSatelliteInstallButton(hass, entry)])


class KioskSatelliteInstallButton(ButtonEntity):
    """Installs or reinstalls the Kiosk Satellite APK on the device."""

    _attr_has_entity_name = True
    _attr_name = "Install Kiosk Satellite"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_install"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )

    async def async_press(self) -> None:
        session = async_get_clientsession(self.hass)
        client = AdbClient(
            self._entry.data[CONF_HOST],
            self._entry.data[CONF_PORT],
            self._entry.data[CONF_KEY_PATH],
        )
        await client.connect()
        try:
            abi = await client.getprop("ro.product.cpu.abi")
            apk_url = await latest_apk_url(session, abi)
            async with session.get(apk_url) as resp:
                resp.raise_for_status()
                data = await resp.read()
            tmp_path = await self.hass.async_add_executor_job(_write_temp_apk, data)
            try:
                await client.push(tmp_path, KS_APK_REMOTE_PATH)
                await client.shell(f"pm install -r -g {KS_APK_REMOTE_PATH}")
                await client.shell(f"rm -f {KS_APK_REMOTE_PATH}")
            finally:
                await self.hass.async_add_executor_job(os.unlink, tmp_path)
        finally:
            await client.close()

        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if coordinator is not None:
            await coordinator.async_request_refresh()
