"""Small shared helpers.

KSM-BEHAVE-009: area_id -> area name resolution. HA's DeviceInfo.suggested_area
expects a name string, but the config flow's AreaSelector returns an area_id
-- resolve once here rather than duplicating the area_registry lookup in
both button.py and sensor.py.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from .const import CONF_AUTO_UPDATE_ALL, MANAGER_ENTRY_KEY


def resolve_area_name(hass: HomeAssistant, area_id: str | None) -> str | None:
    if not area_id:
        return None
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area else None


def auto_update_all_enabled(hass: HomeAssistant) -> bool:
    """KSM-BEHAVE-080: the loaded manager entry's Auto-update all option.

    Off when no manager entry is loaded -- the fleet switch lives there.
    """
    entry_id = hass.data.get(MANAGER_ENTRY_KEY)
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    return bool(entry and entry.options.get(CONF_AUTO_UPDATE_ALL, False))
