"""GitHub releases lookup for the Kiosk Satellite APK (Phase 1 install).

Release assets are published per-ABI -- confirmed live against the real
jxlarrea/kiosk-satellite releases API (tag 2026.9.61):
`kiosk-satellite-<ver>.apk` (universal) plus `.arm64-v8a.apk`/
`.armeabi-v7a.apk`/`.x86_64.apk` splits. KSM installs only the universal
build on every device (#71): one cached file per version instead of one per
ABI, and no device ABI probe.
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


def universal_apk(assets: Iterable[tuple[str, str]]) -> tuple[str, str]:
    """KSM-BEHAVE-107 (#71): (name, url) of the release's universal APK -- the
    `.apk` asset whose name carries no ABI. One file serves every device."""
    for name, url in assets:
        if name.endswith(".apk") and not any(token in name for token in _ABI_TOKENS):
            return name, url
    raise ApkAssetNotFound("no universal .apk asset on the release")


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
        try:
            universal_apk(assets)
        except ApkAssetNotFound:
            continue  # empty or split-only: not installable (#71)
        return release, assets
    raise ApkAssetNotFound(f"no universal .apk asset found in releases of {KS_GITHUB_REPO}")


async def latest_release(session: aiohttp.ClientSession) -> tuple[str, str]:
    """Return (universal_download_url, tag_name) for the latest usable release.

    KSM-BEHAVE-040 (Phase 2, "install and update"): the tag name is the
    target version install_and_launch compares against the device's
    currently-installed versionName to decide whether to preserve a
    compatible install or push a new artifact, and to verify the postcondition
    afterward -- the same tag format already confirmed live to match the
    app's own reported version (docstring above, `docs/SPEC/provisioning.md`).
    """
    release, assets = await _latest_usable_release(session)
    return universal_apk(assets)[1], release.get("tag_name") or ""


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

