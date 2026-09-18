"""Installed Kiosk Satellite version sensor (Phase 1).

Reads the version back from the device's own /api/health -- unauthenticated
(Verified Finding 2) and the only trustworthy read-back channel (Finding 3:
`am start` exit 0 proves nothing).
"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([KioskSatelliteVersionSensor(coordinator, entry)])


class KioskSatelliteVersionSensor(CoordinatorEntity, SensorEntity):
    """Installed Kiosk Satellite app version, read back from /api/health."""

    _attr_has_entity_name = True
    _attr_name = "Kiosk Satellite version"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_version"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("appVersion")
