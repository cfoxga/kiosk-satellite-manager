"""GitHub releases lookup for the Kiosk Satellite APK (Phase 1 install).

Release assets are published per-ABI -- confirmed live against the real
jxlarrea/kiosk-satellite releases API (tag 2026.9.61):
`kiosk-satellite-<ver>.apk` (universal) plus `.arm64-v8a.apk`/
`.armeabi-v7a.apk`/`.x86_64.apk` splits. Each device gets the split for its
own ABI list (#74, reversing #71's universal-only install), so KSM downloads
only the splits its devices run; the universal APK is the fallback for a
device whose ABIs are unknown or match no split.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

import aiohttp

from .const import KS_GITHUB_REPO

_RELEASES_URL = f"https://api.github.com/repos/{KS_GITHUB_REPO}/releases?per_page=10"
_ABI_TOKENS = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
# Kiosk Satellite publishes splits for exactly these (release_apk.dart).
SPLIT_ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64")


@dataclass(frozen=True)
class ReleaseInfo:
    """KSM-BEHAVE-071: what the update entity shows for the latest release."""

    version: str
    url: str | None
    notes: str | None
    # KSM-BEHAVE-107: (filename, download URL) of every .apk asset, for the
    # APK cache to pick a device's split from without a second lookup.
    assets: tuple[tuple[str, str], ...] = ()
    # KSM-BEHAVE-114: a version pinned in global settings, served only from
    # the APK cache (no assets, never downloaded).
    pinned: bool = False
    # KSM-BEHAVE-116: every usable release the check saw, newest first
    # (this one included), for the Install version list and pinned downloads.
    recent: tuple[ReleaseInfo, ...] = ()


class ApkAssetNotFound(Exception):
    """No usable .apk asset on the latest release."""


def is_universal_apk(name: str) -> bool:
    return name.endswith(".apk") and not any(token in name for token in _ABI_TOKENS)


def universal_apk(assets: Iterable[tuple[str, str]]) -> tuple[str, str]:
    """KSM-BEHAVE-107 (#71): (name, url) of the release's universal APK -- the
    `.apk` asset whose name carries no ABI. One file serves every device."""
    for name, url in assets:
        if is_universal_apk(name):
            return name, url
    raise ApkAssetNotFound("no universal .apk asset on the release")


def split_apk(name: str, abi: str) -> bool:
    return name.endswith(f".{abi}.apk")


def select_release_apk(
    assets: Iterable[tuple[str, str]], version: str, abis: Iterable[str]
) -> tuple[str, str]:
    """KSM-BEHAVE-107 (#74): (name, url) of the APK a device with `abis` gets --
    the split for the first of its ABIs, in the device's order, that has one;
    else the universal APK (unknown ABIs, or none with a split)."""
    assets, abis = list(assets), list(abis)
    for abi in abis:
        if abi in SPLIT_ABIS:
            for name, url in assets:
                if split_apk(name, abi):
                    return name, url
    try:
        return universal_apk(assets)
    except ApkAssetNotFound:
        raise ApkAssetNotFound(
            f"release {version} has no APK for ABIs {abis!r}"
        ) from None


async def _usable_releases(session: aiohttp.ClientSession) -> list[tuple[dict, list[tuple[str, str]]]]:
    """Every non-draft, non-prerelease release with an .apk asset, newest
    first, with those assets. Install and the update check (KSM-BEHAVE-071)
    both pick through here, so the entity never advertises a release install
    would skip."""
    async with session.get(
        _RELEASES_URL,
        headers={"Accept": "application/vnd.github+json"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    releases = [data] if isinstance(data, dict) else data
    usable = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        assets = [
            (asset["name"], asset["browser_download_url"])
            for asset in release.get("assets", [])
            if asset.get("name", "").endswith(".apk")
        ]
        if assets:
            usable.append((release, assets))
    if not usable:
        raise ApkAssetNotFound(f"no .apk asset found in releases of {KS_GITHUB_REPO}")
    return usable


async def latest_release(
    session: aiohttp.ClientSession, abis: Iterable[str] = ()
) -> tuple[str, str]:
    """Return (download_url, tag_name) for the latest usable release's APK
    for a device with `abis` (`select_release_apk`).

    KSM-BEHAVE-040 (Phase 2, "install and update"): the tag name is the
    target version install_and_launch compares against the device's
    currently-installed versionName to decide whether to preserve a
    compatible install or push a new artifact, and to verify the postcondition
    afterward -- the same tag format already confirmed live to match the
    app's own reported version (docstring above, `docs/SPEC/provisioning.md`).
    """
    release, assets = (await _usable_releases(session))[0]
    version = release.get("tag_name") or ""
    return select_release_apk(assets, version, abis)[1], version


async def latest_release_info(session: aiohttp.ClientSession) -> ReleaseInfo:
    """KSM-BEHAVE-071: version, page URL and notes of the latest usable
    release, with every usable release in `recent` (KSM-BEHAVE-116).
    ABI-independent -- one lookup serves every managed device."""
    recent = tuple(
        ReleaseInfo(
            version=release.get("tag_name") or "",
            url=release.get("html_url"),
            notes=release.get("body"),
            assets=tuple(assets),
        )
        for release, assets in await _usable_releases(session)
    )
    return replace(recent[0], recent=recent)

