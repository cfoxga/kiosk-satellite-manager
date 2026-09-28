"""Unit tests for the GitHub-releases APK asset picker (KSM-BEHAVE-001)."""
import pytest

from custom_components.kiosk_satellite_manager.ks_api import (
    _RELEASES_URL,
    ApkAssetNotFound,
    latest_release,
    latest_release_info,
    select_release_apk,
    universal_apk,
)
from custom_components.kiosk_satellite_manager.const import KS_GITHUB_REPO

# live-verified shape from the real jxlarrea/kiosk-satellite releases API (tag 2026.9.61)
_ASSETS = [
    ("kiosk-satellite-2026.9.61.arm64-v8a.apk", "https://example.invalid/arm64-v8a.apk"),
    ("kiosk-satellite-2026.9.61.apk", "https://example.invalid/kiosk-satellite-2026.9.61.apk"),
    ("kiosk-satellite-2026.9.61.armeabi-v7a.apk", "https://example.invalid/armeabi-v7a.apk"),
    ("kiosk-satellite-2026.9.61.x86_64.apk", "https://example.invalid/x86_64.apk"),
]
_UNIVERSAL = _ASSETS[1]


def test_universal_apk_ignores_every_split():
    """[KSM-TEST-206] #71: every device gets the universal APK, whatever splits
    the release also carries (listed first here on purpose)."""
    assert universal_apk(_ASSETS) == _UNIVERSAL


def test_universal_apk_raises_without_a_universal_asset():
    """[KSM-TEST-206] negative case: split-only or empty releases have none."""
    split_only = [asset for asset in _ASSETS if asset != _UNIVERSAL]
    with pytest.raises(ApkAssetNotFound):
        universal_apk(split_only)
    with pytest.raises(ApkAssetNotFound):
        universal_apk([])


def _release(assets, *, draft=False, prerelease=False, tag_name=None, html_url=None, body=None):
    return {
        "draft": draft,
        "prerelease": prerelease,
        "tag_name": tag_name,
        "html_url": html_url,
        "body": body,
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


def test_select_release_apk_picks_the_device_split():
    """[KSM-TEST-224] #74: a device gets the split for the first of its ABIs
    that has one, in the device's own order."""
    assert select_release_apk(_ASSETS, "v", ["arm64-v8a", "armeabi-v7a"]) == _ASSETS[0]
    assert select_release_apk(_ASSETS, "v", ["armeabi-v7a", "armeabi"]) == _ASSETS[2]
    assert select_release_apk(_ASSETS, "v", ["x86_64"]) == _ASSETS[3]


def test_select_release_apk_falls_back_to_universal_only_when_needed():
    """[KSM-TEST-224] negative cases: an unknown ABI list, or one no split
    matches, takes the universal APK; with neither available it raises."""
    assert select_release_apk(_ASSETS, "v", []) == _UNIVERSAL
    assert select_release_apk(_ASSETS, "v", ["x86", "mips"]) == _UNIVERSAL
    split_only = [asset for asset in _ASSETS if asset != _UNIVERSAL]
    with pytest.raises(ApkAssetNotFound):
        select_release_apk(split_only, "v", ["x86"])
    with pytest.raises(ApkAssetNotFound):
        select_release_apk([], "v", ["arm64-v8a"])


@pytest.mark.asyncio
async def test_latest_release_skips_empty_releases_and_picks_the_device_split():
    """[KSM-TEST-009] [KSM-TEST-224] a release with no .apk asset is not
    usable; a split-only release is (#74), and the device's split is picked."""
    split_only = [asset for asset in _ASSETS if asset != _UNIVERSAL]
    session = _FakeSession(
        [_release([], tag_name="3"), _release(split_only, tag_name="2"), _release(_ASSETS, tag_name="1")]
    )
    url, tag_name = await latest_release(session, ["armeabi-v7a"])
    assert (url, tag_name) == (split_only[1][1], "2")


@pytest.mark.asyncio
async def test_latest_release_info_keeps_every_usable_release_newest_first():
    """[KSM-TEST-225] #74: the release check keeps each usable release with
    its assets, so the Install version list can offer versions not yet
    downloaded. Negative: drafts, prereleases and asset-less releases are
    not among them."""
    session = _FakeSession(
        [
            _release(_ASSETS, prerelease=True, tag_name="2026.9.91"),
            _release(_ASSETS, tag_name="2026.9.90"),
            _release([], tag_name="2026.9.89"),
            _release(_ASSETS[:1], tag_name="2026.9.88"),
            _release(_ASSETS, draft=True, tag_name="2026.9.87"),
        ]
    )
    info = await latest_release_info(session)
    assert info.version == "2026.9.90"
    assert [r.version for r in info.recent] == ["2026.9.90", "2026.9.88"]
    assert info.recent[1].assets == tuple(_ASSETS[:1])


@pytest.mark.asyncio
async def test_latest_release_skips_draft_and_prerelease():
    session = _FakeSession(
        [
            _release(_ASSETS, draft=True),
            _release(_ASSETS, prerelease=True),
            _release(_ASSETS),
        ]
    )
    url, _ = await latest_release(session)
    assert url == _UNIVERSAL[1]


@pytest.mark.asyncio
async def test_latest_release_handles_single_dict_payload():
    session = _FakeSession(_release(_ASSETS))
    url, _ = await latest_release(session)
    assert url == _UNIVERSAL[1]


@pytest.mark.asyncio
async def test_latest_release_raises_when_no_assets_in_any_release():
    session = _FakeSession([_release([]), _release([])])
    with pytest.raises(ApkAssetNotFound) as err:
        await latest_release(session)
    assert KS_GITHUB_REPO in str(err.value)


@pytest.mark.asyncio
async def test_latest_release_queries_releases_with_github_accept_header():
    session = _FakeSession(_release(_ASSETS))
    await latest_release(session)
    url, kwargs = session.calls[0]
    assert url == _RELEASES_URL
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"


@pytest.mark.asyncio
async def test_latest_release_returns_url_and_tag_name():
    """KSM-BEHAVE-040 (Phase 2, "install and update"): install_and_launch
    needs the release's own tag_name as the version target -- confirmed live
    to match the app's own reported version (ks_api.py docstring)."""
    session = _FakeSession(_release(_ASSETS, tag_name="2026.9.61"))
    url, tag_name = await latest_release(session)
    assert url == _UNIVERSAL[1]
    assert tag_name == "2026.9.61"


@pytest.mark.asyncio
async def test_latest_release_empty_tag_name_falls_back_to_empty_string():
    session = _FakeSession(_release(_ASSETS))
    _, tag_name = await latest_release(session)
    assert tag_name == ""


@pytest.mark.asyncio
async def test_latest_release_info_returns_first_usable_release_metadata():
    """[KSM-TEST-129] The update check reports the release install would pick,
    skipping a prerelease and a release without an .apk asset."""
    session = _FakeSession(
        [
            _release(_ASSETS, prerelease=True, tag_name="2026.9.80"),
            _release([], tag_name="2026.9.79"),
            _release(
                _ASSETS,
                tag_name="2026.9.78",
                html_url="https://example.invalid/releases/2026.9.78",
                body="- fixed things",
            ),
        ]
    )
    info = await latest_release_info(session)
    assert info.version == "2026.9.78"
    assert info.url == "https://example.invalid/releases/2026.9.78"
    assert info.notes == "- fixed things"
    assert session.calls[0][0] == _RELEASES_URL


@pytest.mark.asyncio
async def test_latest_release_info_raises_without_a_usable_release():
    """[KSM-TEST-129] negative case: nothing publishable is an error, not a
    blank version the update entity would treat as current."""
    session = _FakeSession([_release([], tag_name="2026.9.79")])
    with pytest.raises(ApkAssetNotFound):
        await latest_release_info(session)
