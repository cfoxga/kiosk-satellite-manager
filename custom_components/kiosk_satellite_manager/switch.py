"""Per-device opt-in for automatic Kiosk Satellite updates (KSM-BEHAVE-073),
and the manager's fleet-wide Auto-update all switch (KSM-BEHAVE-080).

The state lives in the config entry's options (default off, which also
covers entries created before this switch existed). Toggling only rewrites
that option -- the integration registers no reload-on-options listener, and
the update entity just re-evaluates its auto-update rules when it changes.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_AREA_ID, CONF_AUTO_UPDATE, CONF_AUTO_UPDATE_ALL, CONF_ENTRY_TYPE, DOMAIN,
    ENTRY_TYPE_MANAGER, SIGNAL_AUTO_UPDATE_ALL,
)
from .helpers import resolve_area_name
from . import fleet


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
        async_add_entities([KioskSatelliteAutoUpdateAllSwitch(hass, entry)])
        return
    for device in fleet.platform_devices(hass, entry):
        fleet.add_entities(async_add_entities, device, [KioskSatelliteAutoUpdateSwitch(hass, device)])


class KioskSatelliteAutoUpdateSwitch(SwitchEntity):
    """Install new Kiosk Satellite releases on this device automatically."""

    _attr_has_entity_name = True
    _attr_name = "Auto-update Kiosk Satellite"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_auto_update"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            suggested_area=resolve_area_name(hass, entry.data.get(CONF_AREA_ID)),
        )

    @property
    def is_on(self) -> bool:
        return bool(self._entry.options.get(CONF_AUTO_UPDATE, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)

    def _set(self, value: bool) -> None:
        fleet.update_device(self.hass, self._entry,
                            options={**self._entry.options, CONF_AUTO_UPDATE: value})
        self.async_write_ha_state()


class KioskSatelliteAutoUpdateAllSwitch(SwitchEntity):
    """Auto-update every managed device, whatever its own switch says.

    Rewrites only the manager entry's option; each device's update entity
    reads it alongside its own and re-evaluates on the dispatcher signal.
    """

    _attr_has_entity_name = True
    _attr_name = "Auto-update all"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_auto_update_all"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )

    async def async_added_to_hass(self) -> None:
        # Device entries can finish loading before the manager at startup;
        # let them re-evaluate now that the option is readable.
        if self.is_on:
            async_dispatcher_send(self.hass, SIGNAL_AUTO_UPDATE_ALL)

    @property
    def is_on(self) -> bool:
        return bool(self._entry.options.get(CONF_AUTO_UPDATE_ALL, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)

    def _set(self, value: bool) -> None:
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, CONF_AUTO_UPDATE_ALL: value}
        )
        self.async_write_ha_state()
        async_dispatcher_send(self.hass, SIGNAL_AUTO_UPDATE_ALL)
