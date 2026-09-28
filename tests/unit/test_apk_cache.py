"""KSM's on-host Kiosk Satellite APK cache (KSM-BEHAVE-107/109, OneDev #70)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.kiosk_satellite_manager import apk_cache
from custom_components.kiosk_satellite_manager.apk_signing import ApkSignerVerificationFailed
from custom_components.kiosk_satellite_manager.const import DOMAIN

_VERIFY = "custom_components.kiosk_satellite_manager.apk_cache.verify_ks_apk_signer"


class _FakeHass:
    def __init__(self, config_dir: Path, device_versions=()) -> None:
        self.config = SimpleNamespace(path=lambda *parts: str(config_dir.joinpath(*parts)))
        self.data = {
            DOMAIN: {
                f"entry{i}": SimpleNamespace(data={"appVersion": v} if v else None)
                for i, v in enumerate(device_versions)
            }
        }

    async def async_add_executor_job(self, func, *args):
        return await asyncio.to_thread(func, *args)


def _session(body: bytes = b"apk-bytes", gate: asyncio.Event | None = None):
    async def read():
        if gate is not None:
            await gate.wait()
        return body

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.read = AsyncMock(side_effect=read)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


def _cache_dir(tmp_path: Path) -> Path:
    return tmp_path / ".cache" / DOMAIN / "apks"


_V = "2026.9.88"
_ASSETS = (
    (f"kiosk-satellite-{_V}.arm64-v8a.apk", "https://example.invalid/arm64.apk"),
    (f"kiosk-satellite-{_V}.armeabi-v7a.apk", "https://example.invalid/v7a.apk"),
    (f"kiosk-satellite-{_V}.x86_64.apk", "https://example.invalid/x86_64.apk"),
    (f"kiosk-satellite-{_V}.apk", "https://example.invalid/universal.apk"),
)


async def test_concurrent_requests_share_one_download(tmp_path):
    """[KSM-TEST-207] Two concurrent requests, one GET, one stored file; a
    later request downloads nothing."""
    hass = _FakeHass(tmp_path)
    gate = asyncio.Event()
    session = _session(b"the-apk", gate)
    name, url = _ASSETS[1]
    with patch(_VERIFY) as verify:
        first = asyncio.ensure_future(apk_cache.async_cached_apk(hass, session, _V, name, url))
        second = asyncio.ensure_future(apk_cache.async_cached_apk(hass, session, _V, name, url))
        await asyncio.sleep(0.05)
        gate.set()
        paths = await asyncio.gather(first, second)
        again = await apk_cache.async_cached_apk(hass, session, _V, name, url)

    assert session.get.call_count == 1
    assert session.get.call_args.args[0] == url
    verify.assert_called_once_with(b"the-apk")
    expected = _cache_dir(tmp_path) / _V / name
    assert paths == [expected, expected] and again == expected
    assert expected.read_bytes() == b"the-apk"
    assert sorted(p.name for p in expected.parent.iterdir()) == [name]


async def test_untrusted_signer_stores_nothing(tmp_path):
    """[KSM-TEST-207] negative case: a rejected signer leaves no file, not
    even a .part."""
    hass = _FakeHass(tmp_path)
    name, url = _ASSETS[1]
    with patch(_VERIFY, side_effect=ApkSignerVerificationFailed("untrusted")):
        with pytest.raises(ApkSignerVerificationFailed):
            await apk_cache.async_cached_apk(hass, _session(), _V, name, url)
    root = _cache_dir(tmp_path)
    assert not root.exists() or not any(root.rglob("*"))


@pytest.mark.parametrize(
    ("version", "name"),
    [("../2026.9.88", "x.apk"), ("2026/9", "x.apk"), (_V, "../x.apk"), (_V, "x.txt"), ("..", "x.apk")],
)
async def test_unsafe_version_or_name_is_refused_before_download(tmp_path, version, name):
    """[KSM-TEST-207] negative case: nothing can leave the cache directory."""
    hass = _FakeHass(tmp_path)
    session = _session()
    with pytest.raises(ValueError):
        await apk_cache.async_cached_apk(hass, session, version, name, "https://example.invalid/x")
    session.get.assert_not_called()


_CACHED = [f"2026.9.{n}" for n in range(80, 89)]


def test_versions_to_keep_applies_the_three_rules():
    """[KSM-TEST-208] running versions, the one before the oldest running
    version, and the newest three -- nothing else."""
    keep = apk_cache.versions_to_keep(_CACHED, ["2026.9.84", "2026.9.86", None])
    assert keep == {"2026.9.83", "2026.9.84", "2026.9.86", "2026.9.87", "2026.9.88"}


def test_versions_to_keep_compares_numerically_and_without_devices():
    """[KSM-TEST-208] 100 > 99; no known device versions keeps the newest three."""
    cached = ["2026.9.98", "2026.9.99", "2026.9.100", "2026.9.101", "2026.9.9"]
    assert apk_cache.versions_to_keep(cached, []) == {"2026.9.99", "2026.9.100", "2026.9.101"}
    # A running version that is not cached still anchors rule 2.
    assert apk_cache.versions_to_keep(cached, ["2026.9.100"]) == {
        "2026.9.99", "2026.9.100", "2026.9.101",
    }
    assert apk_cache.versions_to_keep(cached, ["2026.9.10"]) == {
        "2026.9.9", "2026.9.99", "2026.9.100", "2026.9.101",
    }


async def test_prune_deletes_exactly_the_other_versions(tmp_path):
    """[KSM-TEST-208] async_prune reads device versions from the health
    coordinators and removes every other version directory."""
    root = _cache_dir(tmp_path)
    for version in _CACHED:
        (root / version).mkdir(parents=True)
        (root / version / "kiosk-satellite.apk").write_bytes(b"x")
    hass = _FakeHass(tmp_path, device_versions=["2026.9.84", "2026.9.86", None])

    removed = await apk_cache.async_prune(hass)

    kept = {"2026.9.83", "2026.9.84", "2026.9.86", "2026.9.87", "2026.9.88"}
    assert sorted(p.name for p in root.iterdir()) == sorted(kept)
    assert set(removed) == set(_CACHED) - kept


async def test_prune_without_a_cache_directory_is_a_no_op(tmp_path):
    """[KSM-TEST-208] negative case: nothing cached yet, nothing to do."""
    assert await apk_cache.async_prune(_FakeHass(tmp_path)) == []


async def test_release_apk_caches_the_universal_asset(tmp_path):
    """[KSM-TEST-207] #71: async_release_apk takes the universal asset, never
    a split, and stores it under the release version."""
    hass = _FakeHass(tmp_path)
    session = _session(b"universal")
    release = SimpleNamespace(version=_V, assets=_ASSETS, pinned=False)
    with patch(_VERIFY), patch(
        "custom_components.kiosk_satellite_manager.apk_cache.async_get_clientsession",
        return_value=session,
    ):
        path = await apk_cache.async_release_apk(hass, release)
    assert path == _cache_dir(tmp_path) / _V / _ASSETS[-1][0]
    assert session.get.call_args.args[0] == _ASSETS[-1][1]


def test_versions_to_keep_with_nothing_older_than_the_oldest_device():
    """[KSM-TEST-208] rule 2 adds nothing when no cached version predates the
    oldest running one."""
    assert apk_cache.versions_to_keep(_CACHED, ["2026.9.80"]) == {
        "2026.9.80", "2026.9.86", "2026.9.87", "2026.9.88",
    }


async def test_prune_failure_is_logged_not_raised(tmp_path, caplog):
    """[KSM-TEST-208] negative case: a prune that cannot delete never fails
    the install that triggered it."""
    with patch(
        "custom_components.kiosk_satellite_manager.apk_cache._prune",
        side_effect=PermissionError("read-only"),
    ):
        assert await apk_cache.async_prune(_FakeHass(tmp_path)) == []
    assert "Could not prune the KSM APK cache" in caplog.text


def _put(root: Path, version: str, name: str) -> Path:
    path = root / version / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"cached")
    return path


def test_cached_versions_lists_universal_versions_newest_first(tmp_path):
    """[KSM-TEST-219] #72: the Install version choices are the cached versions
    holding a universal APK, newest first. Negative: a split-only directory
    (left by #70) and a stray file are not offered."""
    root = _cache_dir(tmp_path)
    _put(root, "2026.9.87", "kiosk-satellite-2026.9.87.apk")
    _put(root, "2026.9.100", "kiosk-satellite-2026.9.100.apk")
    _put(root, "2026.9.88", "kiosk-satellite-2026.9.88.apk")
    _put(root, "2026.9.86", "kiosk-satellite-2026.9.86.arm64-v8a.apk")
    (root / "stray.txt").write_text("x")
    assert apk_cache.cached_versions(root) == ["2026.9.100", "2026.9.88", "2026.9.87"]
    assert apk_cache.cached_versions(tmp_path / "missing") == []


def test_versions_to_keep_never_drops_the_pinned_version():
    """[KSM-TEST-221] #72: a pinned version survives even when the three
    retention rules alone would delete it."""
    assert "2026.9.81" not in apk_cache.versions_to_keep(_CACHED, ["2026.9.87"])
    assert "2026.9.81" in apk_cache.versions_to_keep(_CACHED, ["2026.9.87"], pinned="2026.9.81")


async def test_prune_keeps_the_manager_pinned_version(tmp_path):
    """[KSM-TEST-221] async_prune reads the pin from the manager options."""
    root = _cache_dir(tmp_path)
    for version in _CACHED:
        _put(root, version, f"kiosk-satellite-{version}.apk")
    hass = _FakeHass(tmp_path, device_versions=["2026.9.87"])
    with patch(
        "custom_components.kiosk_satellite_manager.apk_cache.pinned_version",
        return_value="2026.9.81",
    ):
        await apk_cache.async_prune(hass)
    assert (root / "2026.9.81").is_dir()


async def test_pinned_release_uses_the_cached_file_and_never_downloads(tmp_path):
    """[KSM-TEST-220] a pinned release has no assets: its APK is the cached
    universal file. Negative: an uncached pin fails clearly, no download."""
    root = _cache_dir(tmp_path)
    cached = _put(root, "2026.9.86", "kiosk-satellite-2026.9.86.apk")
    _put(root, "2026.9.86", "kiosk-satellite-2026.9.86.arm64-v8a.apk")
    session = _session()
    hass = _FakeHass(tmp_path)
    with patch(
        "custom_components.kiosk_satellite_manager.apk_cache.async_get_clientsession",
        return_value=session,
    ):
        pinned = SimpleNamespace(version="2026.9.86", assets=(), pinned=True)
        assert await apk_cache.async_release_apk(hass, pinned) == cached
        missing = SimpleNamespace(version="2026.9.10", assets=(), pinned=True)
        with pytest.raises(FileNotFoundError, match="not in the KSM APK cache"):
            await apk_cache.async_release_apk(hass, missing)
    session.get.assert_not_called()
