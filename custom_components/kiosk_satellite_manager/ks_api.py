"""GitHub releases lookup for the Kiosk Satellite APK (Phase 1 install).

Release assets are published per-ABI -- confirmed live against the real
jxlarrea/kiosk-satellite releases API (tag 2026.9.61):
`kiosk-satellite-<ver>.apk` (universal) plus `.arm64-v8a.apk`/
`.armeabi-v7a.apk`/`.x86_64.apk` splits. Pick the split matching the detected
device ABI (a real onn 4K Pro reported `armeabi-v7a` live this session),
falling back to the universal build for anything unmatched.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import aiohttp

from .const import KS_GITHUB_REPO

_RELEASES_URL = f"https://api.github.com/repos/{KS_GITHUB_REPO}/releases?per_page=10"
_ABI_TOKENS = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")


@dataclass(frozen=True)
class ReleaseInfo:
    """KSM-BEHAVE-071: what the update entity shows for the latest release."""

    version: str
    url: str | None
    notes: str | None
    # KSM-BEHAVE-107: (filename, download URL) of every .apk asset, for the
    # APK cache to pick a device's split from without a second lookup.
    assets: tuple[tuple[str, str], ...] = ()


class ApkAssetNotFound(Exception):
    """No usable .apk asset on the latest release."""


# Kiosk Satellite publishes splits for exactly these (release_apk.dart).
_SPLIT_ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64")


def select_release_apk(
    assets: Iterable[tuple[str, str]], version: str, abis: Iterable[str]
) -> tuple[str, str]:
    """KSM-BEHAVE-107: (name, url) of the asset Kiosk Satellite's own updater
    would pick -- the first of the device's ABIs, in its order, with a split
    named for this version, else the universal build."""
    by_name = dict(assets)
    abis = list(abis)
    version = version.removeprefix("v")
    prefixes = (f"kiosk-satellite-v{version}", f"kiosk-satellite-{version}")

    def named(suffix: str) -> tuple[str, str] | None:
        for prefix in prefixes:
            if (url := by_name.get(prefix + suffix)):
                return prefix + suffix, url
        return None

    for abi in abis:
        if abi in _SPLIT_ABIS and (split := named(f".{abi}.apk")):
            return split
    if universal := named(".apk"):
        return universal
    raise ApkAssetNotFound(f"release {version} has no APK for ABIs {abis!r}")


def select_apk_asset(assets: list[tuple[str, str]], abi: str) -> str:
    """assets: [(filename, download_url), ...] for every .apk asset on a release."""
    abi = abi.strip()
    if abi:
        for name, url in assets:
            if abi in name:
                return url
    for name, url in assets:
        if not any(token in name for token in _ABI_TOKENS):
            return url
    if assets:
        return assets[0][1]
    raise ApkAssetNotFound("no .apk asset on the latest release")


async def _latest_usable_release(session: aiohttp.ClientSession) -> tuple[dict, list[tuple[str, str]]]:
    """The first non-draft, non-prerelease release with an .apk asset, and
    those assets. Install and the update check (KSM-BEHAVE-071) both pick
    through here, so the entity never advertises a release install would skip."""
    async with session.get(
        _RELEASES_URL,
        headers={"Accept": "application/vnd.github+json"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    releases = [data] if isinstance(data, dict) else data
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        assets = [
            (asset["name"], asset["browser_download_url"])
            for asset in release.get("assets", [])
            if asset.get("name", "").endswith(".apk")
        ]
        if assets:
            return release, assets
    raise ApkAssetNotFound(f"no .apk asset found in releases of {KS_GITHUB_REPO}")


async def _latest_release_and_asset(session: aiohttp.ClientSession, abi: str) -> tuple[dict, str]:
    release, assets = await _latest_usable_release(session)
    return release, select_apk_asset(assets, abi)


async def latest_apk_url(session: aiohttp.ClientSession, abi: str) -> str:
    """Return the download URL for the release asset matching abi (or the
    universal build if nothing matches)."""
    _, url = await _latest_release_and_asset(session, abi)
    return url


async def latest_release(session: aiohttp.ClientSession, abi: str) -> tuple[str, str]:
    """Return (download_url, tag_name) for the latest usable release.

    KSM-BEHAVE-040 (Phase 2, "install and update"): the tag name is the
    target version install_and_launch compares against the device's
    currently-installed versionName to decide whether to preserve a
    compatible install or push a new artifact, and to verify the postcondition
    afterward -- the same tag format already confirmed live to match the
    app's own reported version (docstring above, `docs/SPEC/provisioning.md`).
    """
    release, url = await _latest_release_and_asset(session, abi)
    return url, release.get("tag_name") or ""


async def latest_release_info(session: aiohttp.ClientSession) -> ReleaseInfo:
    """KSM-BEHAVE-071: version, page URL and notes of the latest usable
    release. ABI-independent -- one lookup serves every managed device."""
    release, assets = await _latest_usable_release(session)
    return ReleaseInfo(
        version=release.get("tag_name") or "",
        url=release.get("html_url"),
        notes=release.get("body"),
        assets=tuple(assets),
    )

