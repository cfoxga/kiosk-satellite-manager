"""KSM's on-host copy of each Kiosk Satellite release APK (KSM-BEHAVE-107/109, #70).

One verified download of each APK a device needs -- its ABI split (#74) --
serves every device that runs it: the update
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
from .const import (
    APK_CACHE_KEEP_LATEST,
    APK_DOWNLOAD_TIMEOUT_S,
    DOMAIN,
    INSTALL_VERSION_CHOICES_MIN,
)
from .helpers import pinned_version
from .ks_api import SPLIT_ABIS, ReleaseInfo, is_universal_apk, select_release_apk, split_apk

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


def _cached_apks(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [p for p in sorted(folder.iterdir()) if p.is_file() and p.name.endswith(".apk")]


def cached_device_apk(root: Path, version: str, abis: Iterable[str]) -> Path | None:
    """KSM-BEHAVE-107 (#74): a cached APK of `version` a device with `abis`
    runs -- a split for one of its ABIs, in its order, else a universal APK
    (a #71 cache holds only these). None when neither is cached."""
    files = _cached_apks(root / version)
    for abi in abis:
        if abi in SPLIT_ABIS:
            if split := next((p for p in files if split_apk(p.name, abi)), None):
                return split
    return next((p for p in files if is_universal_apk(p.name)), None)


def cached_versions(root: Path) -> list[str]:
    """KSM-BEHAVE-114: every cached version holding an APK, newest first."""
    if not root.is_dir():
        return []
    versions = [p.name for p in root.iterdir() if p.is_dir() and _cached_apks(p)]
    return sorted(versions, key=version_key, reverse=True)


def install_version_choices(
    cached: Iterable[str], recent: Iterable[str], minimum: int = INSTALL_VERSION_CHOICES_MIN
) -> list[str]:
    """KSM-BEHAVE-116 (#74): the Install version list -- every cached version,
    then the newest releases not yet downloaded until there are `minimum`,
    newest first."""
    choices = set(cached)
    for version in recent:
        if len(choices) >= minimum:
            break
        choices.add(version)
    return sorted(choices, key=version_key, reverse=True)


async def async_release_apk(
    hass: HomeAssistant, release: ReleaseInfo, abis: Iterable[str]
) -> Path:
    """KSM-BEHAVE-107: the APK of `release` for a device with `abis` (#74) --
    that device's best file (`select_release_apk`), downloaded only if it is
    not cached. A cached universal APK (a #71 cache) is used instead of a
    download. A release with no known assets (a pin the release check no
    longer lists, KSM-BEHAVE-114) is served from the cache only: any cached
    file the device runs."""
    abis = list(abis)
    _checked(release.version, "x.apk")
    root = cache_root(hass)
    if not release.assets:
        cached = await hass.async_add_executor_job(cached_device_apk, root, release.version, abis)
        if cached is None:
            raise FileNotFoundError(f"Kiosk Satellite {release.version} is not in the KSM APK cache")
        return cached
    name, url = select_release_apk(release.assets, release.version, abis)
    best = root / release.version / name
    if not await hass.async_add_executor_job(best.is_file):
        universal = await hass.async_add_executor_job(cached_device_apk, root, release.version, ())
        if universal is not None:
            return universal
    return await async_cached_apk(hass, async_get_clientsession(hass), release.version, name, url)


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
