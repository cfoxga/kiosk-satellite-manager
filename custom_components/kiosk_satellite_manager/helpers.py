"""Small shared helpers.

KSM-BEHAVE-009: area_id -> area name resolution. HA's DeviceInfo.suggested_area
expects a name string, but the config flow's AreaSelector returns an area_id
-- resolve once here rather than duplicating the area_registry lookup in
both button.py and sensor.py.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from .const import (
    CONF_AUTO_UPDATE_ALL,
    CONF_TARGET_VERSION,
    MANAGER_ENTRY_KEY,
    RELEASE_COORDINATOR_KEY,
    TARGET_VERSION_LATEST,
)
from .ks_api import ReleaseInfo


def resolve_area_name(hass: HomeAssistant, area_id: str | None) -> str | None:
    if not area_id:
        return None
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area else None


def _manager_entry_options(hass: HomeAssistant) -> dict:
    entry_id = hass.data.get(MANAGER_ENTRY_KEY)
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    return entry.options if entry else {}


def auto_update_all_enabled(hass: HomeAssistant) -> bool:
    """KSM-BEHAVE-080: the loaded manager entry's Auto-update all option.

    Off when no manager entry is loaded -- the fleet switch lives there.
    """
    return bool(_manager_entry_options(hass).get(CONF_AUTO_UPDATE_ALL, False))


def pinned_version(hass: HomeAssistant) -> str | None:
    """KSM-BEHAVE-114: the Install version pinned in global settings, or None
    for Latest (also when no manager entry is loaded)."""
    version = _manager_entry_options(hass).get(CONF_TARGET_VERSION, TARGET_VERSION_LATEST)
    return None if not version or version == TARGET_VERSION_LATEST else version


def target_release(hass: HomeAssistant) -> ReleaseInfo | None:
    """KSM-BEHAVE-114: what every KSM install aims at -- the pinned version,
    else the shared release check's latest (None before its first success)."""
    if (version := pinned_version(hass)) is not None:
        return ReleaseInfo(
            version,
            None,
            f"Pinned to {version} in Kiosk Satellite Manager global settings.",
            pinned=True,
        )
    release = hass.data.get(RELEASE_COORDINATOR_KEY)
    return release.data if release is not None else None
