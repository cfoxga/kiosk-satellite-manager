"""Keep a kiosk's confirmed ESPHome and Music Assistant devices in its HA Area."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr

from . import esphome_adopt, fleet
from .const import CONF_AREA_ID, CONF_HOST, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _physical_entries(hass: HomeAssistant) -> list:
    return fleet.device_entries(hass) + [
        entry for entry in hass.config_entries.async_entries(DOMAIN)
        if not entry.data.get("entry_type")
    ]


def _music_device(hass: HomeAssistant, name: str) -> dr.DeviceEntry | None:
    """Match one Kiosk Satellite player to one uniquely named KSM device."""
    if sum(entry.title == name for entry in _physical_entries(hass)) != 1:
        return None
    registry = dr.async_get(hass)
    matches = [
        device
        for entry in hass.config_entries.async_entries("music_assistant")
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        if device.disabled_by is None
        and device.manufacturer == "Kiosk Satellite"
        and (device.name_by_user or device.name) == name
        and any(domain == "music_assistant" for domain, _ in device.identifiers)
    ]
    return matches[0] if len(matches) == 1 else None


async def async_reconcile_area(
    hass: HomeAssistant, device: dr.DeviceEntry, *, old_area_id: str | None = None
) -> int:
    """Fill links, following a physical move while respecting a different Area."""
    ident = next((value for domain, value in device.identifiers if domain == DOMAIN), None)
    entry = fleet.resolve_device(hass, ident) if ident else None
    if entry is None:
        return 0
    if entry.data.get(CONF_AREA_ID) != device.area_id:
        fleet.update_device(hass, entry, data={**entry.data, CONF_AREA_ID: device.area_id})
    area_id = device.area_id
    if area_id is None and old_area_id is None:
        return 0
    if area_id is not None and ar.async_get(hass).async_get_area(area_id) is None:
        return 0

    registry = dr.async_get(hass)
    linked: dict[str, dr.DeviceEntry] = {}
    esp_roots: set[str] = set()
    host = entry.data.get(CONF_HOST)
    if host and sum(other.data.get(CONF_HOST) == host for other in _physical_entries(hass)) == 1:
        try:
            esphome_entries = await esphome_adopt._entries_at(hass, host)
        except Exception:  # DNS failure cannot block the Area repair or MA link
            _LOGGER.exception("Could not resolve ESPHome Area link for %s", entry.title)
            esphome_entries = []
        if len(esphome_entries) == 1:
            for sibling in dr.async_entries_for_config_entry(registry, esphome_entries[0].entry_id):
                if sibling.disabled_by is None:
                    linked[sibling.id] = sibling
                    esp_roots.add(sibling.id)
    if music := _music_device(hass, entry.title):
        linked[music.id] = music

    changed = 0
    for sibling in linked.values():
        if sibling.id == device.id:
            continue
        if ((area_id is not None and sibling.area_id is None)
                or (old_area_id is not None and sibling.area_id == old_area_id)):
            if sibling.area_id != area_id:
                registry.async_update_device(sibling.id, area_id=area_id)
                changed += 1

    # HA's Bluetooth discovery registers a separate device beneath the ESPHome
    # proxy, often under a different config entry. Follow only a name-derived
    # ESPHome child of a root that actually reached the target Area.
    if esp_roots:
        all_devices = {
            sibling.id: sibling
            for config in hass.config_entries.async_entries()
            for sibling in dr.async_entries_for_config_entry(registry, config.entry_id)
        }
        for child in all_devices.values():
            parent = registry.async_get(child.via_device_id) if child.via_device_id else None
            if parent is None or parent.id not in esp_roots or parent.area_id != area_id:
                continue
            parent_name = parent.name_by_user or parent.name or ""
            child_name = child.name_by_user or child.name or ""
            if (child.disabled_by is None and child.manufacturer == "esphome"
                    and parent_name and child_name.startswith(f"{parent_name} (")):
                if ((area_id is not None and child.area_id is None)
                        or (old_area_id is not None and child.area_id == old_area_id)):
                    if child.area_id != area_id:
                        registry.async_update_device(child.id, area_id=area_id)
                        changed += 1
    return changed
