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
