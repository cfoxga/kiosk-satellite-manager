"""ADB-enabled binary sensor (KSM-BEHAVE-079).

Polled on its own interval rather than riding the /api/health coordinator:
a device with ADB on but no Kiosk Satellite installed yet has no health
endpoint, and that is exactly when an operator needs to know ADB is up.
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .adb_client import async_probe_adb_port
from .const import (
    ADB_PROBE_INTERVAL_MIN, CONF_AREA_ID, CONF_ENTRY_TYPE, CONF_HOST, CONF_PORT,
    DEFAULT_ADB_PORT, DOMAIN, ENTRY_TYPE_MANAGER,
)
from .helpers import resolve_area_name
from . import fleet, permissions
from .const import PERMISSIONS_POLL_INTERVAL_MIN

SCAN_INTERVAL = timedelta(minutes=ADB_PROBE_INTERVAL_MIN)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
        return
    for device in fleet.platform_devices(hass, entry):
        fleet.add_entities(async_add_entities, device,
                           [KioskSatelliteAdbEnabledSensor(hass, device)], update_before_add=True)
        if permissions.approved_recipe(device) is not None:
            fleet.add_entities(async_add_entities, device,
                               [KioskSatellitePermissionsSensor(hass, device)])


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


class KioskSatellitePermissionsSensor(BinarySensorEntity):
    """KSM-BEHAVE-141: on when a required permission reads back not granted."""

    _attr_has_entity_name = True
    _attr_name = "Permissions"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_permissions"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            suggested_area=resolve_area_name(hass, entry.data.get(CONF_AREA_ID)),
        )

    async def async_added_to_hass(self) -> None:
        monitor = permissions.get_monitor(self.hass, self._entry)
        monitor.listeners.append(self.async_write_ha_state)
        self.async_on_remove(lambda: monitor.listeners.remove(self.async_write_ha_state))
        self.async_on_remove(async_track_time_interval(
            self.hass, self._poll, timedelta(minutes=PERMISSIONS_POLL_INTERVAL_MIN)))
        self.hass.async_create_task(self._poll())

    async def _poll(self, _now=None) -> None:
        await permissions.async_refresh(self.hass, self._entry)

    @property
    def is_on(self) -> bool | None:
        status = permissions.get_monitor(self.hass, self._entry).status
        return None if status is None else status.problem

    @property
    def extra_state_attributes(self) -> dict:
        status = permissions.get_monitor(self.hass, self._entry).status
        if status is None:
            return {}
        return {"missing": list(status.missing), "not_declared": list(status.not_declared)}
