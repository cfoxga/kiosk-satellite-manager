"""Unit tests for resolve_area_name (KSM-BEHAVE-009)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.kiosk_satellite_manager.helpers import resolve_area_name


def test_resolve_area_name_returns_none_when_no_area_id():
    assert resolve_area_name(MagicMock(), None) is None


def test_resolve_area_name_returns_none_when_area_missing_from_registry():
    fake_registry = MagicMock()
    fake_registry.async_get_area.return_value = None
    with patch(
        "custom_components.kiosk_satellite_manager.helpers.ar.async_get",
        return_value=fake_registry,
    ):
        assert resolve_area_name(MagicMock(), "stale-area-id") is None


def test_resolve_area_name_returns_the_area_name():
    fake_area = MagicMock()
    fake_area.name = "Kitchen"
    fake_registry = MagicMock()
    fake_registry.async_get_area.return_value = fake_area
    with patch(
        "custom_components.kiosk_satellite_manager.helpers.ar.async_get",
        return_value=fake_registry,
    ):
        assert resolve_area_name(MagicMock(), "kitchen-id") == "Kitchen"
        fake_registry.async_get_area.assert_called_once_with("kitchen-id")


def test_pinned_release_carries_the_release_check_assets():
    """[KSM-TEST-225] #74: a pinned version the release check still lists
    keeps its assets, so an install can download it. Negative: a version the
    check does not list (or no check yet) has none -- cache only."""
    from types import SimpleNamespace

    from custom_components.kiosk_satellite_manager.const import RELEASE_COORDINATOR_KEY
    from custom_components.kiosk_satellite_manager.helpers import pinned_release
    from custom_components.kiosk_satellite_manager.ks_api import ReleaseInfo

    assets = (("kiosk-satellite-2026.9.86.arm64-v8a.apk", "https://example.invalid/86"),)
    older = ReleaseInfo("2026.9.86", "https://example.invalid/r86", "old notes", assets)
    latest = ReleaseInfo("2026.9.88", None, None, (), recent=(
        ReleaseInfo("2026.9.88", None, None, ()), older,
    ))
    hass = SimpleNamespace(data={RELEASE_COORDINATOR_KEY: SimpleNamespace(data=latest)})
    pinned = pinned_release(hass, "2026.9.86")
    assert (pinned.version, pinned.assets, pinned.pinned) == ("2026.9.86", assets, True)
    unlisted = pinned_release(hass, "2026.9.10")
    assert (unlisted.assets, unlisted.pinned) == ((), True)
    assert pinned_release(SimpleNamespace(data={}), "2026.9.86").assets == ()
