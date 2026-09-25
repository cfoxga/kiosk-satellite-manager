"""Unit tests for the rename helper (KSM-BEHAVE-084/085, #48).

`derive_rename_names`/`derive_dns_host` are pure and tested directly.
`apply_rename_ks_settings` reuses `provisioning.apply_provisioning`'s
patch-then-readback shape (already covered by test_provisioning.py) so
this only pins the settings-based idempotent skip and delegation.
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
_GET_SETTINGS = "custom_components.kiosk_satellite_manager.rename.get_settings"
_APPLY_PROVISIONING = "custom_components.kiosk_satellite_manager.rename.apply_provisioning"


# --- derive_rename_names (KSM-BEHAVE-084) ---------------------------------

def test_derive_rename_names_slugifies_node_name_as_dns_label():
    """[KSM-TEST-162] #56: the node name is the hyphenated DNS label KS puts
    on the wire, never HA's underscore action-name form."""
    names = derive_rename_names("Great Room Device")
    assert names.device_name == "Great Room Device"
    assert names.hostname == "great-room-device"
    assert names.esphome_node_name == "great-room-device"


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

_CURRENT = {
    "device.name": "Kitchen Display",
    "device.hostname": "kitchen-display",
    "esphome.node_name": "kitchen-display",
}


async def test_apply_rename_ks_settings_skips_patch_when_already_applied():
    """[KSM-TEST-175] every derived value already on the device -> no PATCH."""
    names = RenameNames("Kitchen Display", "kitchen-display", "kitchen-display")
    with patch(_GET_SETTINGS, new=AsyncMock(return_value=dict(_CURRENT))) as mock_get, patch(
        _APPLY_PROVISIONING
    ) as mock_apply:
        result = await apply_rename_ks_settings(MagicMock(), "host", "token", names)
    assert result == "unchanged"
    mock_get.assert_awaited_once()
    mock_apply.assert_not_called()


async def test_apply_rename_ks_settings_patches_stale_node_despite_matching_name():
    """[KSM-TEST-175] /api/health carries no node name: a matching display
    name with a stale node (a fresh install's `ks-` default) still PATCHes."""
    names = RenameNames("Kitchen Display", "kitchen-display", "kitchen-display")
    session = MagicMock()
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "Kitchen Display"})), patch(
        _GET_SETTINGS, new=AsyncMock(return_value={**_CURRENT, "esphome.node_name": "ks-kitchen-display"})
    ), patch(_APPLY_PROVISIONING, new=AsyncMock(return_value={"name": "Kitchen Display"})) as mock_apply:
        result = await apply_rename_ks_settings(session, "host", "token", names)
    assert result == "applied"
    mock_apply.assert_awaited_once_with(
        session,
        "host",
        "token",
        {
            "device.name": "Kitchen Display",
            "device.hostname": "kitchen-display",
            "esphome.node_name": "kitchen-display",
        },
    )


@pytest.mark.parametrize("key", ["device.name", "device.hostname"])
async def test_apply_rename_ks_settings_applies_when_name_or_hostname_differs(key):
    names = RenameNames("Kitchen Display", "kitchen-display", "kitchen-display")
    with patch(_GET_SETTINGS, new=AsyncMock(return_value={**_CURRENT, key: "old"})), patch(
        _APPLY_PROVISIONING, new=AsyncMock(return_value={"name": "Kitchen Display"})
    ) as mock_apply:
        result = await apply_rename_ks_settings(MagicMock(), "host", "token", names)
    assert result == "applied"
    mock_apply.assert_awaited_once()


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
