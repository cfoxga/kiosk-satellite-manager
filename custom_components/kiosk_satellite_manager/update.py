"""Kiosk Satellite update entity (KSM-BEHAVE-072) and opt-in auto-update
(KSM-BEHAVE-073).

Installed version is the device's own /api/health appVersion (the entry's
health coordinator); latest version, release page and notes come from the
single shared release check (KSM-BEHAVE-071). Install is
button.async_install_entry -- the same verified sequence as the
Install/Reinstall button -- so an upgrade is pinned, read back and
credential-handled exactly like a manual reinstall.

Auto-update is evaluated on every update from either coordinator and when
the entry's options change (the auto-update switch). It fires only for a
version HA itself reports as an available, unskipped update, only on a
reachable device with no install running, and at most once per version per
load of the entry, so a release that fails to install is not retried on
every poll.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .button import async_install_entry
from .const import CONF_AREA_ID, CONF_AUTO_UPDATE, CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_MANAGER, RELEASE_COORDINATOR_KEY
from .helpers import resolve_area_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
        return
    async_add_entities(
        [
            KioskSatelliteUpdateEntity(
                hass,
                hass.data[DOMAIN][entry.entry_id],
                hass.data[RELEASE_COORDINATOR_KEY],
                entry,
            )
        ]
    )


class KioskSatelliteUpdateEntity(CoordinatorEntity, UpdateEntity):
    """Kiosk Satellite app version on one device vs. the latest release."""

    _attr_has_entity_name = True
    _attr_name = "Kiosk Satellite"
    _attr_title = "Kiosk Satellite"
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(self, hass: HomeAssistant, health, release, entry: ConfigEntry) -> None:
        super().__init__(release)
        self._health = health
        self._entry = entry
        self._auto_attempted: set[str] = set()
        self._attr_unique_id = f"{entry.entry_id}_update"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            suggested_area=resolve_area_name(hass, entry.data.get(CONF_AREA_ID)),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._health.async_add_listener(self._handle_coordinator_update))
        self.async_on_remove(self._entry.add_update_listener(self._async_entry_updated))
        self._maybe_auto_update()

    @property
    def available(self) -> bool:
        # Reachability is the device's, not GitHub's: a failed release check
        # keeps the last good release (the coordinator retains its data).
        return self._health.last_update_success or self._health.ksm_installing

    @property
    def installed_version(self) -> str | None:
        if not self._health.data:
            return None
        return self._health.data.get("appVersion")

    @property
    def latest_version(self) -> str | None:
        return self.coordinator.data.version if self.coordinator.data else None

    @property
    def release_url(self) -> str | None:
        return self.coordinator.data.url if self.coordinator.data else None

    @property
    def in_progress(self) -> bool:
        return bool(self._health.ksm_installing)

    async def async_release_notes(self) -> str | None:
        return self.coordinator.data.notes if self.coordinator.data else None

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        try:
            await async_install_entry(self.hass, self._entry)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                f"Kiosk Satellite install failed on {self._entry.title}: {err}"
            ) from err

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._maybe_auto_update()

    async def _async_entry_updated(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._maybe_auto_update()

    @callback
    def _maybe_auto_update(self) -> None:
        """KSM-BEHAVE-073 trigger rules 1-4."""
        if not self._entry.options.get(CONF_AUTO_UPDATE, False):
            return
        if self.state != STATE_ON:
            return
        if not self._health.last_update_success or self._health.ksm_installing:
            return
        version = self.latest_version
        if version in self._auto_attempted:
            return
        self._auto_attempted.add(version)
        _LOGGER.info(
            "auto-updating Kiosk Satellite on %s from %s to %s",
            self._entry.title,
            self.installed_version,
            version,
        )
        self._entry.async_create_background_task(
            self.hass,
            self._async_auto_install(version),
            f"{DOMAIN} auto-update {self._entry.entry_id}",
        )

    async def _async_auto_install(self, version: str) -> None:
        try:
            await async_install_entry(self.hass, self._entry)
        except Exception as err:  # logged, never retried for this version
            _LOGGER.warning(
                "automatic Kiosk Satellite update to %s failed on %s: %s",
                version,
                self._entry.title,
                err,
            )
