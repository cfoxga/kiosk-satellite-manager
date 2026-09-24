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
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er

from .adb_client import AdbClient, AdbConnectFailed
from .const import (
    CONF_AREA_ID,
    CONF_ENTRY_TYPE,
    CONF_DEVICE_PROFILE,
    CONF_HA_TOKEN,
    CONF_HA_URL,
    CONF_TOKEN_MODE,
    CONF_HOME_LAUNCHER,
    CONF_HOST,
    CONF_KEY_PATH,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
    ENTRY_TYPE_MANAGER,
    MANAGER_UPDATE_RUNNING_KEY,
    RELEASE_COORDINATOR_KEY,
    INSTALL_LAUNCH_POLL_ATTEMPTS,
    INSTALL_LAUNCH_POLL_DELAY_S,
    TOKEN_MODE_AUTO,
)
from .credentials import TokenCredential, async_replace_entry_credential

from .helpers import resolve_area_name
from .install import install_and_launch
from .ks_update import OUTCOME_AWAITING_CONFIRMATION, async_self_update_entry

_LOGGER = logging.getLogger(__name__)


def _adb_unreachable_error(entry: ConfigEntry) -> HomeAssistantError:
    """KSM-BEHAVE-081: an ADB connection failure names ADB and the
    configured host:port, and says what ADB is still needed for -- never a
    bare AdbConnectFailed."""
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    return HomeAssistantError(
        f"Kiosk Satellite ADB is unreachable at {host}:{port} -- ADB is needed only for "
        "onboarding, and for an explicit Install/Reinstall or Uninstall"
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
        async_add_entities([KioskSatelliteUpdateAllButton(hass, entry)])
        return
    async_add_entities(
        [
            KioskSatelliteInstallButton(hass, entry),
            KioskSatelliteUninstallButton(hass, entry),
        ]
    )


async def async_install_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Install/upgrade Kiosk Satellite over ADB on one entry's device.

    The Install/Reinstall button's own sequence -- *superseded 2026-09-24
    (`#47`, KSM-BEHAVE-081/082)*: the update entity's Install and opt-in
    auto-update now run `ks_update.async_self_update_entry` instead (Kiosk
    Satellite's own `:2324` API), since ADB is refused after onboarding.
    This function remains the ADB path used only by the Install/Reinstall
    button and initial onboarding.

    Refuses while this entry already has an install running -- two
    concurrent ADB installs on one device is never intended.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and coordinator.ksm_installing:
        raise HomeAssistantError(
            f"Kiosk Satellite install already in progress on {entry.title}"
        )
    session = async_get_clientsession(hass)
    client = AdbClient(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_KEY_PATH],
    )
    if coordinator is not None:
        coordinator.ksm_installing = True
        coordinator.async_update_listeners()
    try:
        try:
            await client.connect()
        except AdbConnectFailed as err:
            raise _adb_unreachable_error(entry) from err
        try:
            credential = TokenCredential.from_entry_data(entry.data)
            rotate_managed_credential = entry.data.get(CONF_TOKEN_MODE) == TOKEN_MODE_AUTO
            used_token = await install_and_launch(
                hass,
                client,
                session,
                host=entry.data[CONF_HOST],
                device_name=entry.data.get(CONF_NAME, entry.title),
                password=entry.data.get(CONF_PASSWORD),
                # Auto-created credentials are KSM-managed: a recovery
                # press proves the replacement on-device before the helper
                # revokes the previous owned token. Selected credentials
                # are never replaced or revoked.
                ha_token=None if rotate_managed_credential else (credential.access_token if credential else None),
                token_credential=None if rotate_managed_credential else credential,
                home_launcher=entry.data.get(CONF_HOME_LAUNCHER, True),
                device_model=entry.data.get(CONF_DEVICE_PROFILE),
                ha_url=entry.data.get(CONF_HA_URL),
            )

            if used_token and (rotate_managed_credential or not entry.data.get(CONF_HA_TOKEN)):
                await async_replace_entry_credential(hass, entry, used_token)
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
        await async_install_entry(self.hass, self._entry)


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
        except AdbConnectFailed as err:
            raise _adb_unreachable_error(self._entry) from err
        try:
            await client.uninstall_ks()
        finally:
            await client.close()


class KioskSatelliteUpdateAllButton(ButtonEntity):
    """Update eligible managed devices through their existing install path."""

    _attr_has_entity_name = True
    _attr_name = "Update all"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._attr_unique_id = f"{entry.entry_id}_update_all"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )

    def _eligibility(self, entry: ConfigEntry, version: str) -> str | None:
        health = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if health is None or not health.last_update_success:
            return "unreachable"
        if health.ksm_installing:
            return "install in progress"
        registry = er.async_get(self.hass)
        update = next(
            (item for item in er.async_entries_for_config_entry(registry, entry.entry_id)
             if item.domain == "update" and item.unique_id == f"{entry.entry_id}_update"),
            None,
        )
        state = self.hass.states.get(update.entity_id) if update else None
        if not state:
            return "update entity unavailable"
        if state.attributes.get("latest_version") != version:
            return "release changed"
        if state.state != "on":
            return "current, skipped, or unavailable"
        return None

    async def async_press(self) -> None:
        release = self.hass.data.get(RELEASE_COORDINATOR_KEY)
        if self.hass.data.get(MANAGER_UPDATE_RUNNING_KEY):
            self._notify("Update all is already running.")
            return
        if release is None or release.data is None:
            self._notify("No usable Kiosk Satellite release is known.")
            return
        self.hass.data[MANAGER_UPDATE_RUNNING_KEY] = True
        updated, awaiting, skipped, failed = [], [], [], []
        version = release.data.version
        try:
            entries = [
                entry for entry in self.hass.config_entries.async_entries(DOMAIN)
                if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_MANAGER
                and entry.entry_id in self.hass.data.get(DOMAIN, {})
            ]
            for entry in entries:
                reason = self._eligibility(entry, version)
                if reason:
                    skipped.append(f"{entry.title}: {reason}")
                    continue
                # Rechecked for each entry after all earlier installs complete.
                # async_self_update_entry marks this device installing before
                # yielding, closing the same-device race with other actions.
                # KSM-BEHAVE-081: over the Kiosk Satellite API -- never ADB.
                try:
                    outcome = await async_self_update_entry(self.hass, entry)
                except Exception as err:  # continue with the next device
                    failed.append(f"{entry.title}: {err}")
                    continue
                if outcome == OUTCOME_AWAITING_CONFIRMATION:
                    awaiting.append(entry.title)
                else:
                    updated.append(entry.title)
            self._notify(
                f"Release: {version}\n"
                f"Updated: {', '.join(updated) or 'none'}\n"
                f"Awaiting confirmation on device: {', '.join(awaiting) or 'none'}\n"
                f"Skipped: {', '.join(skipped) or 'none'}\n"
                f"Failed: {', '.join(failed) or 'none'}"
            )
        finally:
            self.hass.data.pop(MANAGER_UPDATE_RUNNING_KEY, None)

    def _notify(self, message: str) -> None:
        persistent_notification.async_create(
            self.hass, message=message, title="Kiosk Satellite Update all",
            notification_id=f"{DOMAIN}_update_all",
        )
