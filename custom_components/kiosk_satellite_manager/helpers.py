"""Small shared helpers.

KSM-BEHAVE-009: area_id -> area name resolution. HA's DeviceInfo.suggested_area
expects a name string, but the config flow's AreaSelector returns an area_id
-- resolve once here rather than duplicating the area_registry lookup in
both button.py and sensor.py.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar


def resolve_area_name(hass: HomeAssistant, area_id: str | None) -> str | None:
    if not area_id:
        return None
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area else None
