"""KSM-TEST-120/121: device-management service authorization and payload bounds."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError

from custom_components.kiosk_satellite_manager import (
    SERVICE_CAPABILITY_REPORT,
    SERVICE_ONBOARDING_PLAN,
    SERVICE_PROVISION,
)
from custom_components.kiosk_satellite_manager.const import DOMAIN

from .conftest import init_integration


@pytest.mark.parametrize(
    ("service", "payload", "return_response"),
    [
        (SERVICE_PROVISION, {"settings": {"device.name": "Kitchen"}}, False),
        (SERVICE_CAPABILITY_REPORT, {}, True),
        (SERVICE_ONBOARDING_PLAN, {}, True),
    ],
)
@pytest.mark.parametrize("caller", ["read_only", "ordinary_user", "automation", "unknown_user"])
async def test_device_management_services_reject_untrusted_callers_before_adb(
    hass, service, payload, return_response, caller
):
    """[KSM-TEST-120] Every rejected context fails before client construction."""
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})):
        ctx = await init_integration(hass)

    # HA's first user is always the owner. Seed it so the caller under test
    # receives the intended read-only or ordinary policy.
    await hass.auth.async_create_user("existing owner")
    if caller == "read_only":
        user_id = (await hass.auth.async_create_user("read only", group_ids=[GROUP_ID_READ_ONLY])).id
    elif caller == "ordinary_user":
        user_id = (await hass.auth.async_create_user("ordinary user")).id
    elif caller == "unknown_user":
        user_id = "missing"
    else:
        user_id = None
    with patch(
        "custom_components.kiosk_satellite_manager.AdbClient"
    ) as client_cls:
        with pytest.raises(ServiceValidationError, match="not authorized"):
            await hass.services.async_call(
                DOMAIN,
                service,
                {"config_entry_id": ctx.entry.entry_id, **payload},
                blocking=True,
                return_response=return_response,
                context=Context(user_id=user_id),
            )

    client_cls.assert_not_called()


@pytest.mark.parametrize("is_admin, can_control", [(True, False), (False, True)])
async def test_provision_allows_admin_or_explicit_target_control(hass, is_admin, can_control):
    """[KSM-TEST-120] Authorization is per target, not global service access."""
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})):
        ctx = await init_integration(hass)

    user = SimpleNamespace(is_admin=is_admin, permissions=MagicMock())
    user.permissions.check_entity.return_value = can_control
    with patch.object(hass.auth, "async_get_user", new=AsyncMock(return_value=user)), patch(
        "custom_components.kiosk_satellite_manager.AdbClient"
    ) as client_cls, patch(
        "custom_components.kiosk_satellite_manager.ks_api_login",
        new=AsyncMock(return_value="device-token"),
    ), patch(
        "custom_components.kiosk_satellite_manager.apply_provisioning", new=AsyncMock(return_value={})
    ) as apply:
        client_cls.return_value.connect = AsyncMock()
        client_cls.return_value.close = AsyncMock()
        hass.data[DOMAIN][ctx.entry.entry_id].async_request_refresh = AsyncMock()
        await hass.services.async_call(
            DOMAIN,
            SERVICE_PROVISION,
            {"config_entry_id": ctx.entry.entry_id, "settings": {"device.name": "Kitchen"}},
            blocking=True,
            context=Context(user_id="allowed-user"),
        )

    apply.assert_awaited_once()


@pytest.mark.parametrize(
    "settings",
    [
        {"unexpected.setting": "x"},
        {"remote.enabled": "true"},
        {"device.name": "Kitchen", "unexpected.setting": "x"},
        {},
    ],
)
async def test_provision_rejects_unsupported_or_mistyped_settings_before_adb(hass, settings):
    """[KSM-TEST-121] The public provisioning schema is an explicit typed allowlist."""
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})):
        ctx = await init_integration(hass)

    with patch("custom_components.kiosk_satellite_manager.AdbClient") as client_cls:
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_PROVISION,
                {"config_entry_id": ctx.entry.entry_id, "settings": settings},
                blocking=True,
                context=Context(user_id="admin-user"),
            )
    client_cls.assert_not_called()
