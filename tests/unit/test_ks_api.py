"""Unit tests for the GitHub-releases APK asset picker (KSM-BEHAVE-001)."""
import pytest

from custom_components.kiosk_satellite_manager.ks_api import (
    _RELEASES_URL,
    ApkAssetNotFound,
    latest_apk_url,
    select_apk_asset,
)
from custom_components.kiosk_satellite_manager.const import KS_GITHUB_REPO

# live-verified shape from the real jxlarrea/kiosk-satellite releases API (tag 2026.9.61)
_ASSETS = [
    ("kiosk-satellite-2026.9.61.apk", "https://example.invalid/kiosk-satellite-2026.9.61.apk"),
    ("kiosk-satellite-2026.9.61.arm64-v8a.apk", "https://example.invalid/arm64-v8a.apk"),
    ("kiosk-satellite-2026.9.61.armeabi-v7a.apk", "https://example.invalid/armeabi-v7a.apk"),
    ("kiosk-satellite-2026.9.61.x86_64.apk", "https://example.invalid/x86_64.apk"),
]


def test_picks_matching_abi_split():
    assert select_apk_asset(_ASSETS, "armeabi-v7a") == "https://example.invalid/armeabi-v7a.apk"


def test_falls_back_to_universal_build_for_unmatched_abi():
    assert (
        select_apk_asset(_ASSETS, "mips")
        == "https://example.invalid/kiosk-satellite-2026.9.61.apk"
    )


def test_empty_abi_falls_back_to_universal_build():
    assert (
        select_apk_asset(_ASSETS, "")
        == "https://example.invalid/kiosk-satellite-2026.9.61.apk"
    )


def test_raises_when_no_apk_assets():
    with pytest.raises(ApkAssetNotFound):
        select_apk_asset([], "arm64-v8a")


def _release(assets, *, draft=False, prerelease=False):
    return {
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {"name": name, "browser_download_url": url} for name, url in assets
        ],
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    """Records the releases-API GET and replays a canned payload."""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(self._payload)


@pytest.mark.asyncio
async def test_latest_apk_url_skips_empty_release_to_pick_from_later_release():
    session = _FakeSession([_release([]), _release(_ASSETS)])
    url = await latest_apk_url(session, "arm64-v8a")
    assert url == "https://example.invalid/arm64-v8a.apk"


@pytest.mark.asyncio
async def test_latest_apk_url_skips_draft_and_prerelease():
    session = _FakeSession(
        [
            _release(_ASSETS, draft=True),
            _release(_ASSETS, prerelease=True),
            _release(_ASSETS),
        ]
    )
    url = await latest_apk_url(session, "x86_64")
    assert url == "https://example.invalid/x86_64.apk"


@pytest.mark.asyncio
async def test_latest_apk_url_handles_single_dict_payload():
    session = _FakeSession(_release(_ASSETS))
    url = await latest_apk_url(session, "armeabi-v7a")
    assert url == "https://example.invalid/armeabi-v7a.apk"


@pytest.mark.asyncio
async def test_latest_apk_url_raises_when_no_assets_in_any_release():
    session = _FakeSession([_release([]), _release([])])
    with pytest.raises(ApkAssetNotFound) as err:
        await latest_apk_url(session, "arm64-v8a")
    assert KS_GITHUB_REPO in str(err.value)


@pytest.mark.asyncio
async def test_latest_apk_url_queries_releases_with_github_accept_header():
    session = _FakeSession(_release(_ASSETS))
    await latest_apk_url(session, "arm64-v8a")
    url, kwargs = session.calls[0]
    assert url == _RELEASES_URL
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
