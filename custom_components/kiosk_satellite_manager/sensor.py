"""Installed Kiosk Satellite version sensor (Phase 1).

Reads the version back from the device's own /api/health -- unauthenticated
(Verified Finding 2) and the only trustworthy read-back channel (Finding 3:
`am start` exit 0 proves nothing).

KSM-BEHAVE-007: while the coordinator is flagged "installing" (set by the
Install button / auto-install step around install_and_launch), show a
transitional "Installing" state instead of "unavailable" -- confirmed live
that with no distinct state the sensor just reads unavailable for the whole
install+boot window, which reads as broken rather than in-progress.

KSM-BEHAVE-009: the device's suggested Area (chosen at config-flow time)
carries through to DeviceInfo.suggested_area here too, not just on the
button, since HA only applies device-registry defaults the first time
either entity registers the device.
"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_AREA_ID, DOMAIN
from .helpers import resolve_area_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([KioskSatelliteVersionSensor(hass, coordinator, entry)])


class KioskSatelliteVersionSensor(CoordinatorEntity, SensorEntity):
    """Installed Kiosk Satellite app version, read back from /api/health."""

    _attr_has_entity_name = True
    _attr_name = "Kiosk Satellite version"

    def __init__(self, hass: HomeAssistant, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_version"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            suggested_area=resolve_area_name(hass, entry.data.get(CONF_AREA_ID)),
        )

    @property
    def available(self) -> bool:
        return super().available or bool(getattr(self.coordinator, "ksm_installing", False))

    @property
    def native_value(self) -> str | None:
        if getattr(self.coordinator, "ksm_installing", False):
            return "Installing"
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("appVersion")
