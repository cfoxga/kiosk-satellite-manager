"""Install/Reinstall Kiosk Satellite button entity (Phase 1).

Delegates the fetch/push/install/launch/grant/sync sequence to
install.install_and_launch (KSM-BEHAVE-007/008/010/011) so the config flow's
auto-install step (KSM-BEHAVE-012) can reuse it unchanged. Flags the
coordinator "installing" for the duration so the version sensor can show a
transitional state instead of "Unavailable" (KSM-BEHAVE-007), then polls a
few times after install so the sensor updates without waiting for the next
5-minute cycle.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .adb_client import AdbClient
from .const import (
    CONF_AREA_ID,
    CONF_DEVICE_PROFILE,
    CONF_HA_TOKEN,
    CONF_TOKEN_MODE,
    CONF_HOME_LAUNCHER,
    CONF_HOST,
    CONF_KEY_PATH,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
    INSTALL_LAUNCH_POLL_ATTEMPTS,
    INSTALL_LAUNCH_POLL_DELAY_S,
    TOKEN_MODE_AUTO,
)
from .credentials import TokenCredential, async_replace_entry_credential

from .helpers import resolve_area_name
from .install import install_and_launch

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(
        [
            KioskSatelliteInstallButton(hass, entry),
            KioskSatelliteUninstallButton(hass, entry),
        ]
    )


class KioskSatelliteInstallButton(ButtonEntity):
    """Installs or reinstalls the Kiosk Satellite APK on the device."""

    _attr_has_entity_name = True
    _attr_name = "Install Kiosk Satellite"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_install"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            suggested_area=resolve_area_name(hass, entry.data.get(CONF_AREA_ID)),
        )

    async def async_press(self) -> None:
        session = async_get_clientsession(self.hass)
        client = AdbClient(
            self._entry.data[CONF_HOST],
            self._entry.data[CONF_PORT],
            self._entry.data[CONF_KEY_PATH],
        )
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if coordinator is not None:
            coordinator.ksm_installing = True
            coordinator.async_update_listeners()
        try:
            await client.connect()
            try:
                credential = TokenCredential.from_entry_data(self._entry.data)
                rotate_managed_credential = (
                    self._entry.data.get(CONF_TOKEN_MODE) == TOKEN_MODE_AUTO
                )
                used_token = await install_and_launch(
                    self.hass,
                    client,
                    session,
                    host=self._entry.data[CONF_HOST],
                    device_name=self._entry.data.get(CONF_NAME, self._entry.title),
                    password=self._entry.data.get(CONF_PASSWORD),
                    # Auto-created credentials are KSM-managed: a recovery
                    # press proves the replacement on-device before the helper
                    # revokes the previous owned token. Selected credentials
                    # are never replaced or revoked.
                    ha_token=None if rotate_managed_credential else (credential.access_token if credential else None),
                    token_credential=None if rotate_managed_credential else credential,
                    home_launcher=self._entry.data.get(CONF_HOME_LAUNCHER, True),
                    device_model=self._entry.data.get(CONF_DEVICE_PROFILE),
                )

                if used_token and (rotate_managed_credential or not self._entry.data.get(CONF_HA_TOKEN)):
                    await async_replace_entry_credential(self.hass, self._entry, used_token)
            finally:
                await client.close()

            if coordinator is not None:
                for attempt in range(INSTALL_LAUNCH_POLL_ATTEMPTS):
                    await coordinator.async_request_refresh()
                    if coordinator.last_update_success:
                        break
                    if attempt < INSTALL_LAUNCH_POLL_ATTEMPTS - 1:
                        await asyncio.sleep(INSTALL_LAUNCH_POLL_DELAY_S)
        finally:
            if coordinator is not None:
                coordinator.ksm_installing = False
                coordinator.async_update_listeners()


class KioskSatelliteUninstallButton(ButtonEntity):
    """KSM-BEHAVE-022: removes Kiosk Satellite from the device.

    The cleanup counterpart to the Install/Reinstall button -- on a
    factory-reset or repurposed device the app (and its device-admin
    receiver) must go, and doing that by hand over ADB is exactly the kind
    of one-off manual work this integration exists to remove.
    """

    _attr_has_entity_name = True
    _attr_name = "Uninstall Kiosk Satellite"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_uninstall"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            suggested_area=resolve_area_name(hass, entry.data.get(CONF_AREA_ID)),
        )

    async def async_press(self) -> None:
        client = AdbClient(
            self._entry.data[CONF_HOST],
            self._entry.data[CONF_PORT],
            self._entry.data[CONF_KEY_PATH],
        )
        try:
            await client.connect()
            await client.uninstall_ks()
        finally:
            await client.close()
