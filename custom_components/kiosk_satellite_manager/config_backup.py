"""Device configuration backup and restore (KSM-BEHAVE-104/105/106, #69).

A backup is Kiosk Satellite's own whole-device export (`GET
/api/config/export`), written unchanged to a dated file per device, so it
stays importable through KS's own Settings page as well. Files hold real
secrets (the export does not mask them) and are written 0600 outside `www/`
and `.storage/`.

Only distinct backups are kept: a new export equal to the newest file apart
from `exportedAt` replaces it. Retention then trims to the manager's
Backups to keep, oldest first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from . import ks_api_client
from .const import (
    CONF_BACKUP_INTERVAL_HOURS,
    CONF_BACKUP_KEEP,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_TLS_SPKI,
    DEFAULT_BACKUP_INTERVAL_HOURS,
    DEFAULT_BACKUP_KEEP,
    DOMAIN,
    MANAGER_ENTRY_KEY,
    SIGNAL_BACKUPS_CHANGED,
)
from .ks_api_client import KsApiError

_LOGGER = logging.getLogger(__name__)

EXPORT_KIND = "kiosk-satellite-config"
# KSM-BEHAVE-106: credentials KSM itself depends on -- a restore keeps the
# device's current values so an old backup cannot lock KSM out or revive a
# token KSM has since rotated.
LIVE_CREDENTIAL_KEYS = ("remote.password", "ha.token")
_STAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
_NAME_RE = re.compile(r"^.+_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.json$")
_ERRORS = (KsApiError, aiohttp.ClientError, asyncio.TimeoutError)
_LOCKS_KEY = f"{DOMAIN}_backup_locks"
_SELECTION_KEY = f"{DOMAIN}_backup_selection"


def _now() -> datetime:
    return dt_util.now()


def backup_dir(hass: HomeAssistant, entry: ConfigEntry) -> Path:
    """Keyed by entry ID so a rename never orphans earlier backups."""
    return Path(hass.config.path(DOMAIN, "backups", entry.entry_id))


def _stamp(path: Path) -> datetime | None:
    match = _NAME_RE.match(path.name)
    if match is None:
        return None
    return datetime.strptime(match.group(1), _STAMP_FORMAT).replace(
        tzinfo=dt_util.get_default_time_zone()
    )


def list_backups(directory: Path) -> list[Path]:
    """Backup files only (the naming pattern), newest first."""
    if not directory.is_dir():
        return []
    dated = [(stamp, p) for p in directory.iterdir() if p.is_file() and (stamp := _stamp(p))]
    return [p for _, p in sorted(dated, reverse=True)]


def _comparable(payload: dict) -> str:
    return json.dumps(
        {k: v for k, v in payload.items() if k != "exportedAt"}, sort_keys=True
    )


def _manager_options(hass: HomeAssistant) -> dict:
    entry_id = hass.data.get(MANAGER_ENTRY_KEY)
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    return dict(entry.options) if entry is not None else {}


def _write_backup(directory: Path, name: str, payload: dict, keep: int) -> Path:
    """Write, collapse an identical predecessor, trim to `keep` (executor)."""
    existing = list_backups(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle, indent=2)
    os.chmod(path, 0o600)
    if existing and existing[0] != path:
        try:
            previous = json.loads(existing[0].read_text())
        except (OSError, ValueError):
            previous = None
        if isinstance(previous, dict) and _comparable(previous) == _comparable(payload):
            existing[0].unlink()
    for stale in list_backups(directory)[max(keep, 1):]:
        stale.unlink()
    return path


def selected_backup(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The Configuration backup select's choice, shared with Restore."""
    return hass.data.get(_SELECTION_KEY, {}).get(entry.entry_id)


def set_selected_backup(hass: HomeAssistant, entry: ConfigEntry, name: str | None) -> None:
    hass.data.setdefault(_SELECTION_KEY, {})[entry.entry_id] = name


def _entry_lock(hass: HomeAssistant, entry: ConfigEntry) -> asyncio.Lock:
    return hass.data.setdefault(_LOCKS_KEY, {}).setdefault(entry.entry_id, asyncio.Lock())


async def _login(hass: HomeAssistant, entry: ConfigEntry) -> tuple[aiohttp.ClientSession, str, str, str | None]:
    password = entry.data.get(CONF_PASSWORD)
    if not password:
        raise HomeAssistantError(f"no Kiosk Satellite password stored for {entry.title}")
    session = async_get_clientsession(hass)
    host = entry.data[CONF_HOST]
    pin = entry.data.get(CONF_TLS_SPKI)
    token = await ks_api_client.login(session, host, password, pin=pin)
    return session, host, token, pin


async def _export(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    try:
        session, host, token, pin = await _login(hass, entry)
        payload = await ks_api_client.export_config(session, host, token, pin=pin)
    except _ERRORS as err:
        raise HomeAssistantError(
            f"Kiosk Satellite configuration export failed on {entry.title}: {err}"
        ) from err
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != EXPORT_KIND
        or not isinstance(payload.get("settings"), dict)
    ):
        raise HomeAssistantError(
            f"Kiosk Satellite on {entry.title} returned an unrecognised configuration export"
        )
    return payload


async def _async_backup(hass: HomeAssistant, entry: ConfigEntry) -> tuple[Path, dict]:
    async with _entry_lock(hass, entry):
        payload = await _export(hass, entry)
        keep = int(_manager_options(hass).get(CONF_BACKUP_KEEP, DEFAULT_BACKUP_KEEP))
        name = f"{slugify(entry.title)}_{_now().strftime(_STAMP_FORMAT)}.json"
        path = await hass.async_add_executor_job(
            _write_backup, backup_dir(hass, entry), name, payload, keep
        )
    async_dispatcher_send(hass, SIGNAL_BACKUPS_CHANGED, entry.entry_id)
    return path, payload


async def async_backup_entry(hass: HomeAssistant, entry: ConfigEntry) -> Path:
    """KSM-BEHAVE-104: export one device's configuration to a dated file."""
    path, _ = await _async_backup(hass, entry)
    return path


async def async_restore_entry(
    hass: HomeAssistant, entry: ConfigEntry, filename: str | None = None
) -> None:
    """KSM-BEHAVE-106: import a chosen backup back onto its device."""
    directory = backup_dir(hass, entry)
    backups = await hass.async_add_executor_job(list_backups, directory)
    if filename is None:
        source = backups[0] if backups else None
    else:
        source = next((p for p in backups if p.name == filename), None)
    if source is None:
        named = f" named {filename}" if filename else ""
        raise HomeAssistantError(f"no configuration backup{named} for {entry.title}")
    # Read before the safety backup below: retention may remove the source.
    try:
        payload = json.loads(await hass.async_add_executor_job(source.read_text))
    except (OSError, ValueError) as err:
        raise HomeAssistantError(f"cannot read backup {source.name}: {err}") from err
    if not isinstance(payload, dict) or payload.get("kind") != EXPORT_KIND:
        raise HomeAssistantError(f"{source.name} is not a Kiosk Satellite configuration backup")

    # Safety net first; that export supplies the live credentials too.
    _, current = await _async_backup(hass, entry)
    settings = dict(payload.get("settings") or {})
    for key in LIVE_CREDENTIAL_KEYS:
        if key in current["settings"]:
            settings[key] = current["settings"][key]
    payload = {**payload, "settings": settings}

    try:
        session, host, token, pin = await _login(hass, entry)
        await ks_api_client.import_config(session, host, token, payload, pin=pin)
    except _ERRORS as err:
        raise HomeAssistantError(
            f"Kiosk Satellite configuration restore failed on {entry.title}: {err}"
        ) from err
    _LOGGER.info("Restored Kiosk Satellite configuration on %s from %s", entry.title, source.name)
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        await coordinator.async_request_refresh()


async def async_run_due_backups(hass: HomeAssistant) -> None:
    """KSM-BEHAVE-105: back up every loaded device whose newest backup is at
    least the configured period old (or that has none). Filename dates, not
    a timer, decide -- so restarts neither skip nor multiply backups."""
    hours = int(_manager_options(hass).get(CONF_BACKUP_INTERVAL_HOURS, DEFAULT_BACKUP_INTERVAL_HOURS))
    if hours <= 0:
        return
    cutoff = _now() - timedelta(hours=hours)
    due = []
    for entry_id, coordinator in list(hass.data.get(DOMAIN, {}).items()):
        entry = hass.config_entries.async_get_entry(entry_id)
        # An offline device would otherwise log a warning on every tick; it
        # is picked up on the first tick after health sees it again.
        if entry is None or not coordinator.last_update_success:
            continue
        backups = await hass.async_add_executor_job(list_backups, backup_dir(hass, entry))
        if not backups or _stamp(backups[0]) <= cutoff:
            due.append(entry)

    async def _one(entry: ConfigEntry) -> None:
        try:
            await async_backup_entry(hass, entry)
        except HomeAssistantError as err:
            _LOGGER.warning("Scheduled configuration backup skipped: %s", err)

    await asyncio.gather(*(_one(entry) for entry in due))
