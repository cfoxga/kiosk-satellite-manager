"""Native Home Assistant fleet entry ownership and legacy migration."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from types import MappingProxyType
from urllib.parse import urlsplit
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ENTRY_TYPE, CONF_HOST, CONF_PASSWORD, CONF_TLS_SPKI,
    DOMAIN, ENTRY_TYPE_UNMANAGED, ENTRY_TYPE_FLEET,
)
from . import ks_api_client
from .device_repairs import clear_device_repairs

_OPTIONS_KEY = "_ksm_options"
_STATUS_KEY = "_ksm_fleet_status"
_MIGRATING_KEY = f"{DOMAIN}_migrating_entries"
_MIGRATION_LOCKS_KEY = f"{DOMAIN}_migration_locks"
_DOMAIN_LOADING_KEY = f"{DOMAIN}_loading_migrations"
_RECONCILE_LOCK_KEY = f"{DOMAIN}_fleet_reconcile_lock"
_POLLING_KEY = f"{DOMAIN}_fleet_polling"
_LISTENERS_KEY = f"{DOMAIN}_subentry_listeners"
_REMOVAL_LISTENERS_KEY = f"{DOMAIN}_subentry_removal_listeners"
_READ_OK_KEY = f"{DOMAIN}_fleet_read_ok"
_LOGGER = logging.getLogger(__name__)


def mark_domain_loading(hass: HomeAssistant) -> None:
    """Defer legacy retirement until HA finishes initial domain setup."""
    hass.data[_DOMAIN_LOADING_KEY] = []

    @callback
    def _loaded(event) -> None:
        if event.data.get("component") != DOMAIN:
            return
        unsubscribe()
        pending = hass.data.pop(_DOMAIN_LOADING_KEY, [])
        for old, target in pending:
            schedule_migration(hass, old, target)

    unsubscribe = hass.bus.async_listen(EVENT_COMPONENT_LOADED, _loaded)


def schedule_migration(hass: HomeAssistant, old: ConfigEntry, target: ConfigEntry) -> None:
    """Retire an old entry after HA has reported its setup complete."""
    if (pending := hass.data.get(_DOMAIN_LOADING_KEY)) is not None:
        pending.append((old, target))
        return

    async def _run() -> None:
        await asyncio.sleep(0)
        await async_migrate_legacy_device(hass, old, target)

    hass.async_create_task(_run())


class DeviceEntry:
    """Per-device view of a native HA subentry for existing device code."""

    def __init__(self, hass: HomeAssistant, parent: ConfigEntry, subentry: ConfigSubentry) -> None:
        self.hass = hass
        self.parent = parent
        self.subentry_id = subentry.subentry_id
        self.entry_id = subentry.subentry_id  # stable legacy entity/backup key
        self.domain = DOMAIN

    @property
    def present(self) -> bool:
        """False once HA has removed this subentry (#125)."""
        return self.subentry_id in self.parent.subentries

    @property
    def _subentry(self) -> ConfigSubentry:
        return self.parent.subentries[self.subentry_id]

    @property
    def data(self) -> dict:
        return {k: v for k, v in self._subentry.data.items()
                if k not in (_OPTIONS_KEY, _STATUS_KEY)}

    @property
    def options(self) -> dict:
        return dict(self._subentry.data.get(_OPTIONS_KEY, {}))

    @property
    def title(self) -> str:
        return self._subentry.title

    @property
    def fleet_status(self) -> dict:
        return dict(self._subentry.data.get(_STATUS_KEY, {}))

    def add_update_listener(self, listener: Callable) -> Callable:
        listeners = self.hass.data.setdefault(_LISTENERS_KEY, {}).setdefault(self.entry_id, [])
        listeners.append(listener)
        return lambda: listeners.remove(listener) if listener in listeners else None

    def async_on_unload(self, callback: Callable) -> None:
        self.parent.async_on_unload(callback)

    def async_create_background_task(self, hass: HomeAssistant, coro: Any, name: str) -> None:
        self.parent.async_create_background_task(hass, coro, name)

    def update(self, *, data: dict | None = None, options: dict | None = None,
               title: str | None = None) -> None:
        current = self._subentry
        contents = {**current.data}
        if data is not None:
            for key in tuple(contents):
                if key not in (_OPTIONS_KEY, _STATUS_KEY):
                    contents.pop(key)
            contents.update(data)
        if options is not None:
            contents[_OPTIONS_KEY] = options
        self.hass.config_entries.async_update_subentry(
            self.parent, current, data=contents, title=title or current.title,
        )
        for listener in tuple(self.hass.data.get(_LISTENERS_KEY, {}).get(self.entry_id, [])):
            self.hass.async_create_task(listener(self.hass, self))


def device_entries(hass: HomeAssistant, parent: ConfigEntry | None = None) -> list[DeviceEntry]:
    """Return each physical KSM device exactly once."""
    parents = [parent] if parent is not None else hass.config_entries.async_entries(DOMAIN)
    return [DeviceEntry(hass, item, sub) for item in parents
            if item.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_UNMANAGED or
        item.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_FLEET
            for sub in item.subentries.values() if sub.subentry_type == "device"]


def platform_devices(hass: HomeAssistant, entry: ConfigEntry) -> list[ConfigEntry | DeviceEntry]:
    """Devices hosted by the entry whose platform HA is setting up.

    A subentry added while its parent is still setting up (a startup
    migration or move) has no health coordinator yet; skip it here. The
    move reloads the parent afterwards, which sets it up (#80).
    """
    if entry.data.get(CONF_ENTRY_TYPE) not in (ENTRY_TYPE_UNMANAGED, ENTRY_TYPE_FLEET):
        return [entry]
    loaded = hass.data.get(DOMAIN, {})
    return [device for device in device_entries(hass, entry) if device.entry_id in loaded]


def add_entities(add, device: ConfigEntry | DeviceEntry, entities: list, **kwargs: Any) -> None:
    """Give HA native subentry ownership of every physical device entity."""
    if isinstance(device, DeviceEntry):
        kwargs["config_subentry_id"] = device.subentry_id
    add(entities, **kwargs)


def resolve_device(hass: HomeAssistant, entry_id: str) -> ConfigEntry | DeviceEntry | None:
    """Resolve an old entry ID or stable subentry ID to one physical device."""
    direct = hass.config_entries.async_get_entry(entry_id)
    if direct is not None and direct.domain == DOMAIN and not direct.data.get(CONF_ENTRY_TYPE):
        return direct
    matches = [item for item in device_entries(hass) if item.entry_id == entry_id]
    return matches[0] if len(matches) == 1 else None


def update_device(hass: HomeAssistant, entry: ConfigEntry | DeviceEntry, **changes: Any) -> None:
    """Persist a physical device change through its actual HA owner."""
    if isinstance(entry, DeviceEntry):
        entry.update(**changes)
    else:
        hass.config_entries.async_update_entry(entry, **changes)


def ensure_removal_listener(hass: HomeAssistant, parent: ConfigEntry) -> None:
    """Revoke owned credentials and end the device's repairs (KSM-BEHAVE-154)
    when HA's native UI deletes a subentry."""
    installed = hass.data.setdefault(_REMOVAL_LISTENERS_KEY, {})
    if parent.entry_id in installed:
        return
    prior = dict(parent.subentries)

    async def _updated(_hass: HomeAssistant, current: ConfigEntry) -> None:
        nonlocal prior
        removed = {key: sub for key, sub in prior.items() if key not in current.subentries}
        prior = dict(current.subentries)
        actual_removals = [sub for key, sub in removed.items()
                           if resolve_device(hass, key) is None]
        if not actual_removals:
            return
        from .credentials import TokenCredential, async_revoke_owned_credential
        for sub in actual_removals:
            clear_device_repairs(hass, sub.subentry_id)
            await async_revoke_owned_credential(hass, TokenCredential.from_entry_data(sub.data))
        if hass.config_entries.async_get_entry(parent.entry_id) is not None:
            await hass.config_entries.async_reload(parent.entry_id)

    installed[parent.entry_id] = parent.add_update_listener(_updated)


def _fleet_entry(hass: HomeAssistant, leader_id: str) -> ConfigEntry | None:
    return next((item for item in hass.config_entries.async_entries(DOMAIN)
                 if item.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_FLEET
                 and item.data.get("leader_id") == leader_id), None)


def status_available(hass: HomeAssistant, entry_id: str) -> bool:
    """A persisted observation is live only after a successful read this run."""
    return entry_id in hass.data.get(_READ_OK_KEY, set())


def _status_from_response(response: dict) -> dict:
    """Keep only the bounded, non-secret facts used for membership and status."""
    if response.get("ok") is not True or not isinstance(response.get("data"), dict):
        raise ValueError("fleetStatus command did not return data")
    body = response["data"]
    identity = body.get("self")
    if not isinstance(identity, dict) or not isinstance(identity.get("id"), str) or not identity["id"]:
        raise ValueError("fleetStatus omitted self.id")
    leading = body.get("leader")
    if type(leading) is not bool:
        raise ValueError("fleetStatus omitted leader state")
    following = body.get("following")
    leader_id = None
    if following is not None:
        if not isinstance(following, dict) or not isinstance(following.get("leader"), dict):
            raise ValueError("fleetStatus following leader malformed")
        leader_id = following["leader"].get("id")
        if not isinstance(leader_id, str) or not leader_id:
            raise ValueError("fleetStatus following leader omitted id")
    if leading and leader_id or leader_id == identity["id"]:
        raise ValueError("fleetStatus self and leader IDs contradict")
    followers = body.get("followers")
    rows = followers if isinstance(followers, list) else None
    managed_rows = {
        row["id"]: {"phase": row.get("phase"), "online": row.get("online"),
                    # KSM-BEHAVE-146: enough to offer an unmanaged follower.
                    "name": row.get("name"), "address": row.get("address"), "port": row.get("port")}
        for row in (rows or [])[:100]
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    return {
        "self_id": identity["id"],
        "leading": leading,
        "following_id": leader_id,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "pending_invitations": (sum(1 for row in rows if isinstance(row, dict)
                                    and row.get("phase") == "pending") if rows is not None else None),
        "outdated": (len(body["outdated"]) if isinstance(body.get("outdated"), list) else None),
        "blocked": (sum(1 for row in rows if isinstance(row, dict)
                        and row.get("phase") in ("error", "version")) if rows is not None else None),
        "phase": ("leading" if leading else "following" if leader_id else "unmanaged"),
        "follower_rows": managed_rows if rows is not None else None,
    }


def _host_url(response: dict) -> str | None:
    """KS's admin address by name (KSM-BEHAVE-202), kept only as a bare http(s) origin."""
    value = (response.get("data") or {}).get("hostUrl") if response.get("ok") is True else None
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 -- raises on a malformed port
    except ValueError:
        return None
    if (parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        return None
    return f"{parts.scheme}://{parts.netloc}"


async def _read_host_url(session, host: str, token: str, pin: str | None) -> str | None:
    """A failed `fleet` read never fails the status read; it only drops the address."""
    try:
        return _host_url(await ks_api_client.run_command(session, host, token, "fleet", pin=pin))
    except Exception as err:  # older KS, offline mid-poll, or malformed reply
        _LOGGER.debug("KS fleet address unavailable on %s: %s", host, type(err).__name__)
        return None


async def async_poll_device(hass: HomeAssistant, entry_id: str) -> None:
    """Read authenticated KS status once; failed reads retain confirmed placement."""
    active = hass.data.setdefault(_POLLING_KEY, set())
    if entry_id in active:
        return
    active.add(entry_id)
    try:
        device = resolve_device(hass, entry_id)
        if not isinstance(device, DeviceEntry):
            return
        password = device.data.get(CONF_PASSWORD)
        if not password:
            return
        session = async_get_clientsession(hass)
        host, pin = device.data[CONF_HOST], device.data.get(CONF_TLS_SPKI)
        try:
            token = await ks_api_client.login(session, host, password, pin=pin)
            response = await ks_api_client.run_command(session, host, token, "fleetStatus", pin=pin)
            status = _status_from_response(response)
            status["host_url"] = await _read_host_url(session, host, token, pin)
        except Exception as err:  # unavailable or malformed status cannot change ownership
            hass.data.setdefault(_READ_OK_KEY, set()).discard(entry_id)
            _LOGGER.warning("Fleet status unavailable on %s: %s", device.title, type(err).__name__)
            return
        device = resolve_device(hass, entry_id)
        if not isinstance(device, DeviceEntry):
            return
        current = device.parent.subentries[device.subentry_id]
        hass.config_entries.async_update_subentry(
            device.parent, current, data={**current.data, _STATUS_KEY: status}
        )
        hass.data.setdefault(_READ_OK_KEY, set()).add(entry_id)
        await async_reconcile(hass)
        from . import follower_offers, follower_updates  # local: both import fleet
        await follower_updates.async_sync(hass)
        await follower_offers.async_sync(hass)
    finally:
        active.discard(entry_id)


async def _move_device(hass: HomeAssistant, device: DeviceEntry, target: ConfigEntry) -> None:
    """Wait for HA platform setup before changing a subentry's owner."""
    source = device.parent
    if source.entry_id == target.entry_id:
        return
    first, second = sorted((source, target), key=lambda item: item.entry_id)
    async with first.setup_lock:
        async with second.setup_lock:
            current = resolve_device(hass, device.entry_id)
            if not isinstance(current, DeviceEntry):
                return
            if current.parent.entry_id == target.entry_id:
                return
            _transfer_device_rows(hass, current, target)
            source = current.parent
    await hass.config_entries.async_reload(source.entry_id)
    await hass.config_entries.async_reload(target.entry_id)


def _transfer_device_rows(hass: HomeAssistant, device: DeviceEntry, target: ConfigEntry) -> None:
    """Transfer HA ownership while retaining the stable subentry and registry IDs."""
    source = device.parent
    if source.entry_id == target.entry_id:
        return
    subentry = source.subentries[device.subentry_id]
    if subentry.subentry_id in target.subentries:
        raise ValueError("destination already owns device subentry ID")
    hass.config_entries.async_add_subentry(target, subentry)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    old_entities = [row for row in er.async_entries_for_config_entry(entities, source.entry_id)
                    if row.config_subentry_id == subentry.subentry_id]
    old_devices = [row for row in dr.async_entries_for_config_entry(devices, source.entry_id)
                   if row.config_subentry_id == subentry.subentry_id]
    try:
        for row in old_entities:
            entities.async_update_entity(row.entity_id, config_entry_id=target.entry_id,
                                         config_subentry_id=subentry.subentry_id)
        for row in old_devices:
            devices.async_update_device(row.id, new_config_entry_id=target.entry_id,
                                        new_config_subentry_id=subentry.subentry_id)
        if any(entities.async_get(row.entity_id) is None for row in old_entities):
            raise RuntimeError("entity lost during fleet move")
        if any(devices.async_get(row.id) is None for row in old_devices):
            raise RuntimeError("device lost during fleet move")
        hass.config_entries.async_remove_subentry(source, subentry.subentry_id)
    except Exception:
        for row in old_entities:
            if entities.async_get(row.entity_id):
                entities.async_update_entity(row.entity_id, config_entry_id=source.entry_id,
                                             config_subentry_id=subentry.subentry_id)
        for row in old_devices:
            current = devices.async_get(row.id)
            if current and current.config_entry_id != source.entry_id:
                devices.async_update_device(row.id, new_config_entry_id=source.entry_id,
                                            new_config_subentry_id=subentry.subentry_id)
        hass.config_entries.async_remove_subentry(target, subentry.subentry_id)
        raise


async def async_reconcile(hass: HomeAssistant) -> None:
    """Recompute placement from confirmed self/following IDs, serially."""
    lock = hass.data.setdefault(_RECONCILE_LOCK_KEY, asyncio.Lock())
    async with lock:
        devices = device_entries(hass)
        identity_counts = Counter(item.fleet_status.get("self_id") for item in devices
                                  if item.fleet_status.get("self_id"))
        duplicate_ids = {identity for identity, count in identity_counts.items() if count > 1}
        if duplicate_ids:
            _LOGGER.error("Conflicting Kiosk Satellite fleet identities on %s managed devices",
                          sum(identity_counts[identity] for identity in duplicate_ids))
        leaders = {status["self_id"]: device for device in devices
                   if (status := device.fleet_status).get("leading") is True
                   and status.get("self_id") and status["self_id"] not in duplicate_ids}
        for leader_id, leader in leaders.items():
            if _fleet_entry(hass, leader_id) is None and status_available(hass, leader.entry_id):
                await hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_IMPORT},
                    data={CONF_ENTRY_TYPE: ENTRY_TYPE_FLEET, "leader_id": leader_id,
                          "leader_name": leader.title, "offer_followers": True},
                )
            fleet = _fleet_entry(hass, leader_id)
            if fleet is not None and fleet.title != f"Fleet - {leader.title}":
                hass.config_entries.async_update_entry(fleet, title=f"Fleet - {leader.title}")
        for device in devices:
            status = device.fleet_status
            if not status.get("observed_at"):
                continue
            if status.get("self_id") in duplicate_ids or status.get("following_id") in duplicate_ids:
                continue
            leader_id = status.get("self_id") if status.get("leading") else status.get("following_id")
            target = (_fleet_entry(hass, leader_id) if leader_id in leaders else None)
            target = target or unmanaged_entry(hass)
            if target is not None and device.parent.entry_id != target.entry_id:
                await _move_device(hass, device, target)
        for entry in list(hass.config_entries.async_entries(DOMAIN)):
            if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_FLEET or entry.subentries:
                continue
            former = [item for item in device_entries(hass)
                      if item.fleet_status.get("self_id") == entry.data.get("leader_id")]
            if former and all(item.fleet_status.get("leading") is False for item in former) and not any(
                item.fleet_status.get("following_id") == entry.data.get("leader_id")
                for item in device_entries(hass)
            ):
                await hass.config_entries.async_remove(entry.entry_id)


def unmanaged_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Return the one synthetic Unmanaged grouping, if it exists."""
    return next((
        item for item in hass.config_entries.async_entries(DOMAIN)
        if item.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_UNMANAGED
    ), None)


def migration_in_progress(hass: HomeAssistant, entry_id: str) -> bool:
    """Suppress credential revocation only during an ownership transfer."""
    return entry_id in hass.data.get(_MIGRATING_KEY, set())


async def async_migrate_legacy_device(
    hass: HomeAssistant, old: ConfigEntry, target: ConfigEntry,
) -> ConfigSubentry:
    """Serialize duplicate startup discoveries of one legacy device."""
    # Removal takes the entry's setup lock; wait for its own setup to finish.
    async with old.setup_lock:
        pass
    locks = hass.data.setdefault(_MIGRATION_LOCKS_KEY, {})
    lock = locks.setdefault(old.entry_id, asyncio.Lock())
    async with lock:
        if hass.config_entries.async_get_entry(old.entry_id) is None:
            existing = target.subentries.get(old.entry_id)
            if existing is None:
                raise RuntimeError("legacy entry vanished without a destination subentry")
            return existing
        return await _async_migrate_legacy_device(hass, old, target)


async def _async_migrate_legacy_device(
    hass: HomeAssistant, old: ConfigEntry, target: ConfigEntry,
) -> ConfigSubentry:
    """Move one old device entry to a same-ID subentry without recreating rows.

    HA registry rows are moved entity first, device second. Moving the device
    first makes HA delete entities still owned by the old config entry.
    """
    subentry = target.subentries.get(old.entry_id)
    created = subentry is None
    if created:
        subentry = ConfigSubentry(
            data=MappingProxyType({**old.data, _OPTIONS_KEY: dict(old.options)}),
            subentry_id=old.entry_id,
            subentry_type="device",
            title=old.title,
            unique_id=f"legacy:{old.entry_id}",
        )
        hass.config_entries.async_add_subentry(target, subentry)

    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    old_entities = list(er.async_entries_for_config_entry(entities, old.entry_id))
    old_devices = list(dr.async_entries_for_config_entry(devices, old.entry_id))
    try:
        for entity in old_entities:
            entities.async_update_entity(
                entity.entity_id, config_entry_id=target.entry_id,
                config_subentry_id=subentry.subentry_id,
            )
        for device in old_devices:
            devices.async_update_device(
                device.id, new_config_entry_id=target.entry_id,
                new_config_subentry_id=subentry.subentry_id,
            )
        if any(entities.async_get(item.entity_id) is None for item in old_entities):
            raise RuntimeError("entity registry row vanished during fleet migration")
        if any(devices.async_get(item.id) is None for item in old_devices):
            raise RuntimeError("device registry row vanished during fleet migration")
        hass.data.setdefault(_MIGRATING_KEY, set()).add(old.entry_id)
        try:
            await hass.config_entries.async_remove(old.entry_id)
        finally:
            hass.data[_MIGRATING_KEY].discard(old.entry_id)
    except Exception:
        # Keep the old entry usable if an API step fails before retirement.
        if hass.config_entries.async_get_entry(old.entry_id) is not None:
            for entity in old_entities:
                if entities.async_get(entity.entity_id) is not None:
                    entities.async_update_entity(
                        entity.entity_id, config_entry_id=old.entry_id,
                        config_subentry_id=None,
                    )
            for device in old_devices:
                current = devices.async_get(device.id)
                if current is not None and current.config_entry_id != old.entry_id:
                    devices.async_update_device(
                        device.id, new_config_entry_id=old.entry_id,
                        new_config_subentry_id=None,
                    )
            if created:
                hass.config_entries.async_remove_subentry(target, subentry.subentry_id)
        raise
    await hass.config_entries.async_reload(target.entry_id)
    return subentry
