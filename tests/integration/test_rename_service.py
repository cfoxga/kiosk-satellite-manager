"""Rename device service integration tests (KSM-BEHAVE-084/085, #48,
KSM-TEST-161-164)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError

from custom_components.kiosk_satellite_manager import SERVICE_RENAME_DEVICE
from custom_components.kiosk_satellite_manager.const import CONF_HOST, CONF_NAME, DOMAIN
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

from .conftest import admin_context, init_integration

_ADB_CLIENT = "custom_components.kiosk_satellite_manager.AdbClient"
_LOGIN = "custom_components.kiosk_satellite_manager.ks_api_login"
_APPLY_KS = "custom_components.kiosk_satellite_manager.apply_rename_ks_settings"
_RESOLVE_DNS = "custom_components.kiosk_satellite_manager.resolve_and_verify_dns_host"
_FETCH_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"


async def _call_rename(hass, entry_id: str, name: str, context: Context):
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_RENAME_DEVICE,
        {"config_entry_id": entry_id, "name": name},
        blocking=True,
        context=context,
        return_response=True,
    )


async def test_rename_device_full_success_updates_entry_and_host(hass):
    """[KSM-TEST-163/164] verified rename updates entry title/name and,
    once the DNS candidate resolves and verifies, CONF_HOST."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: "old-device.devices.example.com"})

        with patch(_ADB_CLIENT) as mock_client_cls, patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(_APPLY_KS, new=AsyncMock(return_value="applied")), patch(
            _RESOLVE_DNS, new=AsyncMock(return_value=True)
        ):
            result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    mock_client_cls.assert_not_called()
    assert result == {
        "ks": "applied",
        "android": "unsupported",
        "entry": "applied",
        "host": "applied",
    }
    assert ctx.entry.title == "Great Room Device"
    assert ctx.entry.data[CONF_NAME] == "Great Room Device"
    assert ctx.entry.data[CONF_HOST] == "great-room-device.devices.example.com"


async def test_rename_device_repeated_call_is_idempotent(hass):
    """[KSM-TEST-164] a second call with the same already-applied name
    reports unchanged/unchanged rather than re-mutating."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(
            hass,
            data={
                CONF_HOST: "great-room-device.devices.example.com",
                CONF_NAME: "Great Room Device",
            },
        )
        hass.config_entries.async_update_entry(ctx.entry, title="Great Room Device")

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
            _APPLY_KS, new=AsyncMock(return_value="unchanged")
        ):
            result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["ks"] == "unchanged"
    assert result["entry"] == "unchanged"
    assert result["host"] == "unchanged"


async def test_rename_device_unresolved_dns_candidate_reports_pending_and_keeps_host(hass):
    """[KSM-TEST-163] DNS candidate that doesn't resolve to the same device
    leaves CONF_HOST unchanged and reports host: pending."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: "old-device.devices.example.com"})

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
            _APPLY_KS, new=AsyncMock(return_value="applied")
        ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)):
            result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["host"] == "pending"
    assert ctx.entry.data[CONF_HOST] == "old-device.devices.example.com"


async def test_rename_device_ip_host_is_unchanged_not_pending(hass):
    """[KSM-TEST-163] an IP-address host has no DNS label to migrate."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: "192.168.99.99"})

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
            _APPLY_KS, new=AsyncMock(return_value="applied")
        ):
            result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["host"] == "unchanged"
    assert ctx.entry.data[CONF_HOST] == "192.168.99.99"


async def test_rename_device_ks_failure_returns_partial_result_not_raise(hass):
    """[KSM-TEST-162] a KS-layer failure is a returned result, not a raise --
    the caller (HAM) needs the structured partial result to react."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass)

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
            _APPLY_KS, new=AsyncMock(side_effect=KsApiError("device rejected settings: ['device.name']"))
        ):
            result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["ks"] == "failed"
    assert "error" in result
    assert ctx.entry.data.get(CONF_NAME) != "Great Room Device"


async def test_rename_device_rejects_blank_slug_name(hass):
    """[KSM-TEST-162] negative: a name producing an empty slug is rejected
    before any device I/O."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass)

        with patch(_ADB_CLIENT) as mock_client_cls, patch(_LOGIN) as mock_login:
            with pytest.raises(ServiceValidationError):
                await _call_rename(hass, ctx.entry.entry_id, "!!!", await admin_context(hass))
    mock_client_cls.assert_not_called()
    mock_login.assert_not_called()


async def test_rename_device_rejects_unknown_config_entry(hass):
    """[KSM-TEST-161] negative: an unknown target causes zero device I/O."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "irrelevant"})):
        await init_integration(hass)

        with patch(_LOGIN) as mock_login:
            with pytest.raises(ServiceValidationError):
                await _call_rename(hass, "does-not-exist", "Great Room Device", await admin_context(hass))
    mock_login.assert_not_called()
