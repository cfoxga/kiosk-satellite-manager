"""KSM's on-host copy of each Kiosk Satellite release APK (KSM-BEHAVE-107/109, #70).

One verified download of each release's universal APK serves every device: the update
sequence uploads it over the `:2324` API (KSM-BEHAVE-108) and the ADB
Install/Reinstall pushes it. Files live under `<config>/.cache/`, which Home
Assistant's own and Supervisor backups both exclude.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
import logging
import os
from pathlib import Path
import re
import shutil

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .apk_signing import verify_ks_apk_signer
from .const import APK_CACHE_KEEP_LATEST, APK_DOWNLOAD_TIMEOUT_S, DOMAIN
from .helpers import pinned_version
from .ks_api import ReleaseInfo, is_universal_apk, universal_apk

_LOGGER = logging.getLogger(__name__)

_LOCKS_KEY = f"{DOMAIN}_apk_cache_locks"
_PRUNE_LOCK_KEY = f"{DOMAIN}_apk_cache_prune_lock"
_SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def cache_root(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(".cache", DOMAIN, "apks"))


def version_key(version: str) -> tuple:
    """Dot-separated parts compared numerically, so 2026.9.100 > 2026.9.99.
    A non-numeric part sorts after every numeric one at its position."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"[.\-+]", version.removeprefix("v"))
    )


def _checked(version: str, name: str) -> None:
    for part in (version, name):
        if not _SAFE_PART.match(part) or ".." in part:
            raise ValueError(f"unsafe APK cache path part: {part!r}")
    if not name.endswith(".apk"):
        raise ValueError(f"not an APK asset name: {name!r}")


def _verify_and_store(data: bytes, path: Path) -> None:
    # KSM-BEHAVE-063/107: the signer pin runs before anything is written.
    verify_ks_apk_signer(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)


async def async_cached_apk(
    hass: HomeAssistant, session: aiohttp.ClientSession, version: str, name: str, url: str
) -> Path:
    """The stored file for this release asset, downloading and verifying it
    first when it is not cached. Concurrent calls share one download."""
    _checked(version, name)
    path = cache_root(hass) / version / name
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(_LOCKS_KEY, {})
    async with locks.setdefault(str(path), asyncio.Lock()):
        if await hass.async_add_executor_job(path.is_file):
            return path
        _LOGGER.info("Downloading Kiosk Satellite %s (%s) into the KSM cache", version, name)
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=APK_DOWNLOAD_TIMEOUT_S)
        ) as resp:
            resp.raise_for_status()
            data = await resp.read()
        await hass.async_add_executor_job(_verify_and_store, data, path)
    await async_prune(hass)  # a new version may push an old one out
    return path


def cached_universal_apk(root: Path, version: str) -> Path | None:
    """The cached universal APK of `version`, if any (#71 leaves only these;
    a #70 split-only directory has none)."""
    folder = root / version
    if not folder.is_dir():
        return None
    return next(
        (p for p in sorted(folder.iterdir()) if p.is_file() and is_universal_apk(p.name)), None
    )


def cached_versions(root: Path) -> list[str]:
    """KSM-BEHAVE-114: every cached version with a universal APK, newest first."""
    if not root.is_dir():
        return []
    versions = [
        p.name for p in root.iterdir() if p.is_dir() and cached_universal_apk(root, p.name)
    ]
    return sorted(versions, key=version_key, reverse=True)


async def async_release_apk(hass: HomeAssistant, release: ReleaseInfo) -> Path:
    """KSM-BEHAVE-107: the cached universal APK of `release` (#71). A pinned
    release (KSM-BEHAVE-114) is served from the cache only, never downloaded."""
    if release.pinned:
        return await async_pinned_apk(hass, release.version)
    name, url = universal_apk(release.assets)
    return await async_cached_apk(hass, async_get_clientsession(hass), release.version, name, url)


async def async_pinned_apk(hass: HomeAssistant, version: str) -> Path:
    """KSM-BEHAVE-114: the cached file of a pinned version; never a download."""
    _checked(version, "x.apk")
    apk = await hass.async_add_executor_job(cached_universal_apk, cache_root(hass), version)
    if apk is None:
        raise FileNotFoundError(f"Kiosk Satellite {version} is not in the KSM APK cache")
    return apk


def versions_to_keep(
    cached: Iterable[str],
    device_versions: Iterable[str | None],
    latest_n: int = APK_CACHE_KEEP_LATEST,
    *,
    pinned: str | None = None,
) -> set[str]:
    """KSM-BEHAVE-109: the cached versions to keep --
    1. every version a device runs;
    2. the newest cached version older than the oldest of those;
    3. the newest `latest_n` cached versions;
    plus the version pinned in global settings (KSM-BEHAVE-114)."""
    ordered = sorted(set(cached), key=version_key)
    keep = set(ordered[-latest_n:]) if latest_n > 0 else set()
    if pinned in ordered:
        keep.add(pinned)
    running = {v for v in device_versions if v}
    keep |= running & set(ordered)
    if running:
        oldest = version_key(min(running, key=version_key))
        older = [v for v in ordered if version_key(v) < oldest]
        if older:
            keep.add(older[-1])
    return keep


def _prune(root: Path, running: list[str | None], pinned: str | None = None) -> list[str]:
    if not root.is_dir():
        return []
    cached = [p.name for p in root.iterdir() if p.is_dir()]
    keep = versions_to_keep(cached, running, pinned=pinned)
    removed = sorted(set(cached) - keep, key=version_key)
    for version in removed:
        shutil.rmtree(root / version)
    return removed


async def async_prune(hass: HomeAssistant) -> list[str]:
    """KSM-BEHAVE-109: delete every cached version the retention rule does
    not keep. Device versions are each health coordinator's last read
    `appVersion`; a device never read since startup contributes none."""
    running = [
        coordinator.data.get("appVersion")
        for coordinator in hass.data.get(DOMAIN, {}).values()
        if isinstance(getattr(coordinator, "data", None), dict)
    ]
    lock = hass.data.setdefault(_PRUNE_LOCK_KEY, asyncio.Lock())
    try:
        async with lock:
            removed = await hass.async_add_executor_job(
                _prune, cache_root(hass), running, pinned_version(hass)
            )
    except OSError as err:  # never fails the install that triggered it
        _LOGGER.warning("Could not prune the KSM APK cache: %s", err)
        return []
    if removed:
        _LOGGER.info("Pruned Kiosk Satellite %s from the KSM APK cache", ", ".join(removed))
    return removed
