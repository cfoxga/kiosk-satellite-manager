"""Hide follower updates (KSM-BEHAVE-133, #95).

With the manager option on, the ESPHome update entity of every confirmed
fleet follower is disabled (disabled_by=integration), so only leaders prompt
for a Kiosk Satellite release. KSM records the entities it disabled and only
ever re-enables those: an entity the operator or ESPHome disabled is left
alone. Nothing is deleted and no non-update entity is touched.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.helpers.storage import Store

from .const import CONF_HIDE_FOLLOWER_UPDATES, DOMAIN
from .helpers import _manager_entry_options
from . import fleet
from .rename import find_esphome_link

_LOGGER = logging.getLogger(__name__)

_STORE_KEY = f"{DOMAIN}_hidden_follower_updates"
_STORE_CACHE = f"{DOMAIN}_hidden_follower_updates_store"
_LOCK_KEY = f"{DOMAIN}_hidden_follower_updates_lock"


def _hide_enabled(hass: HomeAssistant) -> bool:
    return bool(_manager_entry_options(hass).get(CONF_HIDE_FOLLOWER_UPDATES, False))


async def _desired(hass: HomeAssistant) -> set[str]:
    """Update entities of ESPHome devices whose KSM device is a confirmed follower."""
    if not _hide_enabled(hass):
        return set()
    registry = er.async_get(hass)
    wanted: set[str] = set()
    for device in fleet.device_entries(hass):
        if not device.fleet_status.get("following_id"):
            continue
        link = await find_esphome_link(hass, device.data["host"])
        if link is None:
            continue
        for item in er.async_entries_for_config_entry(registry, link.entry.entry_id):
            if item.domain == "update":
                wanted.add(item.entity_id)
    return wanted


async def async_sync(hass: HomeAssistant) -> None:
    """Make the registry match the option and the confirmed fleet status."""
    lock = hass.data.setdefault(_LOCK_KEY, asyncio.Lock())
    async with lock:
        store = hass.data.setdefault(_STORE_CACHE, Store(hass, 1, _STORE_KEY))
        recorded = set((await store.async_load() or {}).get("entity_ids", []))
        wanted = await _desired(hass)
        registry = er.async_get(hass)
        changed = False
        for entity_id in wanted:
            item = registry.async_get(entity_id)
            if item is not None and item.disabled_by is None:
                registry.async_update_entity(entity_id, disabled_by=RegistryEntryDisabler.INTEGRATION)
                recorded.add(entity_id)
                changed = True
        for entity_id in recorded - wanted:
            item = registry.async_get(entity_id)
            if item is not None and item.disabled_by is RegistryEntryDisabler.INTEGRATION:
                registry.async_update_entity(entity_id, disabled_by=None)
            recorded.discard(entity_id)
            changed = True
        if changed:
            await store.async_save({"entity_ids": sorted(recorded)})


@callback
def async_setup(hass: HomeAssistant):
    """Sync now and whenever an update entity is newly registered (ESPHome may
    register its entity after KSM's sync). Returns the unsubscribe callback."""

    @callback
    def _registered(event: Event) -> None:
        if (event.data.get("action") == "create"
                and str(event.data.get("entity_id", "")).startswith("update.")
                and _hide_enabled(hass)):
            hass.async_create_task(async_sync(hass))

    hass.async_create_task(async_sync(hass))
    return hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _registered)
