"""Kiosk Satellite Manager integration setup.

Skeleton only — Phase 1 (ADB connect/detect/install) has not landed yet. See
the phased plan in the ham-harness repo's kiosk-satellite-manager/ silo,
docs/SPEC/provisioning.md.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

__all__ = ["DOMAIN", "async_setup_entry", "async_unload_entry"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry. No platforms yet — Phase 1 adds button/sensor."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
