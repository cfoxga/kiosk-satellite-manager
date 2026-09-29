"""Configuration backup picker for Restore configuration (KSM-BEHAVE-106).

Options are this entry's own backup filenames, newest first -- a restore
source is never an arbitrary path. Refreshed whenever a backup is written.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import config_backup, fleet
from .const import CONF_AREA_ID, CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_MANAGER, SIGNAL_BACKUPS_CHANGED
from .helpers import resolve_area_name


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
        return
    for device in fleet.platform_devices(hass, entry):
        fleet.add_entities(async_add_entities, device, [KioskSatelliteConfigBackupSelect(hass, device)])


class KioskSatelliteConfigBackupSelect(SelectEntity):
    """Which saved configuration Restore configuration imports."""

    _attr_has_entity_name = True
    _attr_name = "Configuration backup"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:backup-restore"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_config_backup"
        self._attr_options = []
        self._attr_current_option = None
        self._newest_regular: str | None = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            suggested_area=resolve_area_name(hass, entry.data.get(CONF_AREA_ID)),
        )

    async def async_added_to_hass(self) -> None:
        await self._async_reload_options()

        @callback
        def _changed(entry_id: str) -> None:
            if entry_id == self._entry.entry_id:
                self.hass.async_create_task(self._async_reload_options(write=True))

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_BACKUPS_CHANGED, _changed)
        )

    async def _async_reload_options(self, write: bool = False) -> None:
        backups = await self.hass.async_add_executor_job(
            config_backup.list_backups, config_backup.backup_dir(self.hass, self._entry)
        )
        self._attr_options = [p.name for p in backups]
        # A new regular backup becomes the default; a pruned choice falls back
        # to it. A pre-restore safety copy is never the default (#100).
        newest = config_backup.default_backup(self._attr_options)
        if (write and newest != self._newest_regular) or (
            self._attr_current_option not in self._attr_options
        ):
            self._attr_current_option = newest
        self._newest_regular = newest
        config_backup.set_selected_backup(self.hass, self._entry, self._attr_current_option)
        if write:
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise ServiceValidationError(f"{option} is not a configuration backup of {self._entry.title}")
        self._attr_current_option = option
        config_backup.set_selected_backup(self.hass, self._entry, option)
        self.async_write_ha_state()
