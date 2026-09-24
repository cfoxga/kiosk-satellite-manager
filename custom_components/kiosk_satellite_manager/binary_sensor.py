"""ADB-enabled binary sensor (KSM-BEHAVE-079).

Polled on its own interval rather than riding the /api/health coordinator:
a device with ADB on but no Kiosk Satellite installed yet has no health
endpoint, and that is exactly when an operator needs to know ADB is up.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .adb_client import async_probe_adb_port
from .const import (
    ADB_PROBE_INTERVAL_MIN, CONF_AREA_ID, CONF_ENTRY_TYPE, CONF_HOST, CONF_PORT,
    DEFAULT_ADB_PORT, DOMAIN, ENTRY_TYPE_MANAGER,
)
from .helpers import resolve_area_name

SCAN_INTERVAL = timedelta(minutes=ADB_PROBE_INTERVAL_MIN)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
        return
    async_add_entities([KioskSatelliteAdbEnabledSensor(hass, entry)], update_before_add=True)


class KioskSatelliteAdbEnabledSensor(BinarySensorEntity):
    """On when the device's ADB port accepts a TCP connection."""

    _attr_has_entity_name = True
    _attr_name = "ADB enabled"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._host = entry.data[CONF_HOST]
        self._port = entry.data.get(CONF_PORT, DEFAULT_ADB_PORT)
        self._attr_unique_id = f"{entry.entry_id}_adb_enabled"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            suggested_area=resolve_area_name(hass, entry.data.get(CONF_AREA_ID)),
        )

    async def async_update(self) -> None:
        self._attr_is_on = await async_probe_adb_port(self._host, self._port)
