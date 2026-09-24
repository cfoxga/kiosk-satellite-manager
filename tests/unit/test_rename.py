"""Unit tests for the rename helper (KSM-BEHAVE-084/085, #48).

`derive_rename_names`/`derive_dns_host` are pure and tested directly.
`apply_rename_ks_settings` reuses `provisioning.apply_provisioning`'s
patch-then-readback shape (already covered by test_provisioning.py) so
this only pins the idempotent-skip and delegation behavior.
`resolve_and_verify_dns_host` fakes DNS via a stand-in `hass` executor.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.kiosk_satellite_manager.rename import (
    RenameNames,
    apply_rename_ks_settings,
    derive_dns_host,
    derive_rename_names,
    resolve_and_verify_dns_host,
    set_android_device_name,
)

_FETCH_HEALTH = "custom_components.kiosk_satellite_manager.rename.fetch_health"
_APPLY_PROVISIONING = "custom_components.kiosk_satellite_manager.rename.apply_provisioning"


# --- derive_rename_names (KSM-BEHAVE-084) ---------------------------------

def test_derive_rename_names_slugifies_and_underscores():
    names = derive_rename_names("Great Room Device")
    assert names.device_name == "Great Room Device"
    assert names.hostname == "great-room-device"
    assert names.esphome_node_name == "great_room_device"


def test_derive_rename_names_strips_display_name_whitespace():
    names = derive_rename_names("  Kitchen Display  ")
    assert names.device_name == "Kitchen Display"


def test_derive_rename_names_collapses_runs_of_punctuation():
    names = derive_rename_names("Bob's  Room!!")
    assert names.hostname == "bob-s-room"


@pytest.mark.parametrize("bad_name", ["", "   ", "!!!", "---"])
def test_derive_rename_names_rejects_empty_slug(bad_name):
    with pytest.raises(ValueError):
        derive_rename_names(bad_name)


# --- derive_dns_host (KSM-BEHAVE-085) -------------------------------------

def test_derive_dns_host_replaces_first_label_only():
    assert derive_dns_host("old-name.devices.example.com", "new-name") == "new-name.devices.example.com"


def test_derive_dns_host_returns_none_for_ip_host():
    assert derive_dns_host("192.168.1.42", "new-name") is None


def test_derive_dns_host_returns_none_for_bare_hostname():
    assert derive_dns_host("localhost", "new-name") is None


# --- apply_rename_ks_settings (KSM-BEHAVE-084) ----------------------------

async def test_apply_rename_ks_settings_skips_patch_when_already_applied():
    names = RenameNames("Kitchen Display", "kitchen-display", "kitchen_display")
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "Kitchen Display"})), patch(
        _APPLY_PROVISIONING
    ) as mock_apply:
        result = await apply_rename_ks_settings(MagicMock(), "host", "token", names)
    assert result == "unchanged"
    mock_apply.assert_not_called()


async def test_apply_rename_ks_settings_applies_when_name_differs():
    names = RenameNames("Kitchen Display", "kitchen-display", "kitchen_display")
    session = MagicMock()
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})), patch(
        _APPLY_PROVISIONING, new=AsyncMock(return_value={"name": "Kitchen Display"})
    ) as mock_apply:
        result = await apply_rename_ks_settings(session, "host", "token", names)
    assert result == "applied"
    mock_apply.assert_awaited_once_with(
        session,
        "host",
        "token",
        {
            "device.name": "Kitchen Display",
            "device.hostname": "kitchen-display",
            "esphome.node_name": "kitchen_display",
        },
    )


# --- set_android_device_name (KSM-BEHAVE-084) -----------------------------

async def test_set_android_device_name_reports_unsupported():
    names = RenameNames("Kitchen Display", "kitchen-display", "kitchen_display")
    assert await set_android_device_name(names) == "unsupported"


# --- resolve_and_verify_dns_host (KSM-BEHAVE-085) -------------------------

def _fake_hass(resolutions: dict):
    async def _executor_job(func, host):
        if host not in resolutions:
            raise OSError(f"no such host: {host}")
        return resolutions[host]

    hass = MagicMock()
    hass.async_add_executor_job = _executor_job
    return hass


async def test_resolve_and_verify_dns_host_true_when_same_ip_and_health_ok():
    hass = _fake_hass({"old.example.com": "10.0.0.5", "new.example.com": "10.0.0.5"})
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "ok"})):
        assert await resolve_and_verify_dns_host(hass, MagicMock(), "old.example.com", "new.example.com")


async def test_resolve_and_verify_dns_host_false_when_different_ip():
    hass = _fake_hass({"old.example.com": "10.0.0.5", "new.example.com": "10.0.0.6"})
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "ok"})):
        assert not await resolve_and_verify_dns_host(hass, MagicMock(), "old.example.com", "new.example.com")


async def test_resolve_and_verify_dns_host_false_when_candidate_unresolvable():
    hass = _fake_hass({"old.example.com": "10.0.0.5"})
    assert not await resolve_and_verify_dns_host(hass, MagicMock(), "old.example.com", "new.example.com")


async def test_resolve_and_verify_dns_host_false_when_health_unreachable():
    import aiohttp

    hass = _fake_hass({"old.example.com": "10.0.0.5", "new.example.com": "10.0.0.5"})
    with patch(_FETCH_HEALTH, new=AsyncMock(side_effect=aiohttp.ClientError("unreachable"))):
        assert not await resolve_and_verify_dns_host(hass, MagicMock(), "old.example.com", "new.example.com")
