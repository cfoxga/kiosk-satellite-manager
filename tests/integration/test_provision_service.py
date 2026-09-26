"""Provision service integration test (KSM-BEHAVE-083, #47). `AdbClient` is
never constructed -- the service logs in with the entry's stored password
and applies over `PATCH /api/settings`; `fetch_health` is patched to
control read-back, proving both the success path and the
loud-failure-on-mismatch path. The patch-then-readback shape itself is
covered by unit/test_provisioning.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.kiosk_satellite_manager import SERVICE_PROVISION
from custom_components.kiosk_satellite_manager.const import CONF_PASSWORD, DOMAIN
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

from .conftest import admin_context, init_integration

_ADB_CLIENT = "custom_components.kiosk_satellite_manager.AdbClient"
_LOGIN = "custom_components.kiosk_satellite_manager.ks_api_login"
_APPLY = "custom_components.kiosk_satellite_manager.apply_provisioning"


async def test_provision_applies_and_refreshes_on_match(hass):
    """[KSM-TEST-159] one PATCH /api/settings, no AdbClient constructed."""
    health_responses = iter([{"name": "old"}, {"name": "Kitchen Portal"}])

    async def fake_fetch_health(session, host, *, pin=None):
        return next(health_responses)

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass)

        with patch(_ADB_CLIENT) as mock_client_cls, patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ) as mock_login, patch(
            _APPLY, new=AsyncMock(return_value={"name": "Kitchen Portal"})
        ) as mock_apply:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_PROVISION,
                {
                    "config_entry_id": ctx.entry.entry_id,
                    "settings": {"device.name": "Kitchen Portal"},
                },
                blocking=True,
                context=await admin_context(hass),
            )

    mock_login.assert_awaited_once()
    mock_apply.assert_awaited_once()
    mock_client_cls.assert_not_called()


async def test_provision_raises_service_validation_error_on_mismatch(hass):
    from custom_components.kiosk_satellite_manager.provisioning import ProvisioningMismatch

    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"name": "unchanged"}),
    ):
        ctx = await init_integration(hass)

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
            _APPLY, new=AsyncMock(side_effect=ProvisioningMismatch("readback did not match")),
        ):
            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    DOMAIN,
                    SERVICE_PROVISION,
                    {
                        "config_entry_id": ctx.entry.entry_id,
                        "settings": {"device.name": "Kitchen Portal"},
                    },
                    blocking=True,
                    context=await admin_context(hass),
                )


async def test_provision_rejects_a_device_side_setting_rejection(hass):
    """[KSM-TEST-159] negative case: patch_settings' own rejection surfaces
    as ServiceValidationError naming the key."""
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"name": "unchanged"}),
    ):
        ctx = await init_integration(hass)

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
            _APPLY, new=AsyncMock(side_effect=KsApiError("device rejected settings: ['device.name']")),
        ):
            with pytest.raises(ServiceValidationError, match="device.name"):
                await hass.services.async_call(
                    DOMAIN,
                    SERVICE_PROVISION,
                    {
                        "config_entry_id": ctx.entry.entry_id,
                        "settings": {"device.name": "Kitchen Portal"},
                    },
                    blocking=True,
                    context=await admin_context(hass),
                )


async def test_provision_rejects_unknown_config_entry(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"name": "irrelevant"}),
    ):
        await init_integration(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_PROVISION,
            {"config_entry_id": "does-not-exist", "settings": {"device.name": "x"}},
            blocking=True,
            context=await admin_context(hass),
        )


async def test_provision_rejects_an_entry_with_no_stored_password(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"name": "irrelevant"}),
    ):
        ctx = await init_integration(hass, data={CONF_PASSWORD: None})

    with patch(_ADB_CLIENT) as mock_client_cls:
        with pytest.raises(ServiceValidationError, match="no Kiosk Satellite password"):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_PROVISION,
                {
                    "config_entry_id": ctx.entry.entry_id,
                    "settings": {"device.name": "Kitchen Portal"},
                },
                blocking=True,
                context=await admin_context(hass),
            )
    mock_client_cls.assert_not_called()
