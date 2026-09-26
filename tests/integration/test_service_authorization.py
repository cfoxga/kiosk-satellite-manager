"""KSM-TEST-120/121: device-management service authorization and payload bounds."""
from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.auth.models import Group
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockUser
from homeassistant.exceptions import ServiceValidationError

from custom_components.kiosk_satellite_manager import (
    SERVICE_CAPABILITY_REPORT,
    SERVICE_ONBOARDING_PLAN,
    SERVICE_PROVISION,
    SERVICE_RENAME_DEVICE,
)
from custom_components.kiosk_satellite_manager.const import CONF_HOST, CONF_KEY_PATH, CONF_NAME, CONF_PORT, DOMAIN

from .conftest import init_integration


@pytest.mark.parametrize(
    ("service", "payload", "return_response"),
    [
        (SERVICE_PROVISION, {"settings": {"device.name": "Kitchen"}}, False),
        (SERVICE_CAPABILITY_REPORT, {}, True),
        (SERVICE_ONBOARDING_PLAN, {}, True),
        (SERVICE_RENAME_DEVICE, {"name": "Great Room Device"}, True),
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


_MANAGEMENT_SERVICES = [
    (SERVICE_PROVISION, {"settings": {"device.name": "Allowed device"}}, False),
    (SERVICE_CAPABILITY_REPORT, {}, True),
    (SERVICE_ONBOARDING_PLAN, {}, True),
    (SERVICE_RENAME_DEVICE, {"name": "Allowed device"}, True),
]


@pytest.fixture
async def two_devices(hass):
    """Two real active entries and their registered action buttons."""
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})):
        first = await init_integration(hass, data={CONF_HOST: "192.168.99.11"})
        second = await init_integration(hass, data={CONF_HOST: "192.168.99.12"})
        registry = er.async_get(hass)
        buttons = []
        for ctx in (first, second):
            entries = er.async_entries_for_config_entry(registry, ctx.entry.entry_id)
            action_buttons = [entry.entity_id for entry in entries if entry.domain == "button"]
            assert action_buttons, "authorization denial must not rely on an empty registry"
            assert all(hass.states.get(entity_id) is not None for entity_id in action_buttons)
            buttons.append(action_buttons[0])
        assert buttons[0] != buttons[1]
        yield first, second, buttons


def _user_with_control(hass, entity_id):
    # Real HA policy evaluation: no blanket or entity-aware check_entity mock.
    group = Group(name="One device only", policy={
        "entities": {"entity_ids": {entity_id: {"read": True, "control": True}}},
    })
    user = MockUser(groups=[group]).add_to_hass(hass)
    assert not user.is_admin
    assert user.permissions.check_entity(entity_id, POLICY_CONTROL)
    return user


@pytest.fixture
def device_io():
    """Only device I/O is replaced; service dispatch and authorization stay real."""
    prefix = "custom_components.kiosk_satellite_manager."
    with ExitStack() as stack:
        adb = stack.enter_context(patch(prefix + "AdbClient"))
        adb.return_value.connect = AsyncMock()
        adb.return_value.close = AsyncMock()
        collector = stack.enter_context(patch(prefix + "CapabilityReportCollector"))
        collector.return_value.collect = AsyncMock(return_value={"audit": "report"})
        stack.enter_context(patch(prefix + "build_onboarding_plan", return_value={"audit": "plan"}))
        login = stack.enter_context(patch(prefix + "ks_api_login", new=AsyncMock(return_value="test-token")))
        provision = stack.enter_context(patch(prefix + "apply_provisioning", new=AsyncMock(return_value={})))
        rename = stack.enter_context(patch(prefix + "apply_rename_ks_settings", new=AsyncMock(return_value="applied")))
        yield SimpleNamespace(adb=adb, login=login, provision=provision, rename=rename)


@pytest.mark.parametrize("service,payload,return_response", _MANAGEMENT_SERVICES)
async def test_management_services_enforce_permission_for_requested_device(
    hass, two_devices, device_io, service, payload, return_response
):
    """[KSM-TEST-120] A-only control cannot authorize B; A still succeeds."""
    first, second, buttons = two_devices
    user = _user_with_control(hass, buttons[0])
    assert not user.permissions.check_entity(buttons[1], POLICY_CONTROL)
    context = Context(user_id=user.id)
    untouched = dict(second.entry.data)

    with pytest.raises(ServiceValidationError, match="not authorized"):
        await hass.services.async_call(
            DOMAIN, service, {"config_entry_id": second.entry.entry_id, **payload},
            blocking=True, return_response=return_response, context=context,
        )
    device_io.adb.assert_not_called()
    device_io.login.assert_not_awaited()
    device_io.provision.assert_not_awaited()
    device_io.rename.assert_not_awaited()
    assert second.entry.data == untouched

    result = await hass.services.async_call(
        DOMAIN, service, {"config_entry_id": first.entry.entry_id, **payload},
        blocking=True, return_response=return_response, context=context,
    )
    assert second.entry.data == untouched
    if service in (SERVICE_CAPABILITY_REPORT, SERVICE_ONBOARDING_PLAN):
        device_io.adb.assert_called_once_with(
            first.entry.data[CONF_HOST], first.entry.data[CONF_PORT], first.entry.data[CONF_KEY_PATH],
        )
        device_io.adb.return_value.connect.assert_awaited_once()
        device_io.adb.return_value.close.assert_awaited_once()
        assert result["report"] == {"audit": "report"}
        if service == SERVICE_ONBOARDING_PLAN:
            assert result["plan"] == {"audit": "plan"}
    else:
        device_io.login.assert_awaited_once()
        assert device_io.login.await_args.args[1] == first.entry.data[CONF_HOST]
        if service == SERVICE_PROVISION:
            device_io.provision.assert_awaited_once()
            assert device_io.provision.await_args.args[3] == payload["settings"]
        else:
            assert result["entry"] == "applied"
            assert first.entry.data[CONF_NAME] == "Allowed device"
            device_io.rename.assert_awaited_once()


@pytest.mark.parametrize("service,payload,return_response", _MANAGEMENT_SERVICES)
async def test_non_button_control_does_not_authorize_device_management(
    hass, two_devices, device_io, service, payload, return_response
):
    """[KSM-TEST-120] Controlling a sensor does not grant the action button."""
    first, _, buttons = two_devices
    entries = er.async_entries_for_config_entry(er.async_get(hass), first.entry.entry_id)
    sensors = [entry.entity_id for entry in entries if entry.domain == "sensor"]
    assert sensors
    user = _user_with_control(hass, sensors[0])
    assert not user.permissions.check_entity(buttons[0], POLICY_CONTROL)
    with pytest.raises(ServiceValidationError, match="not authorized"):
        await hass.services.async_call(
            DOMAIN, service, {"config_entry_id": first.entry.entry_id, **payload},
            blocking=True, return_response=return_response, context=Context(user_id=user.id),
        )
    device_io.adb.assert_not_called()
    device_io.login.assert_not_awaited()
    device_io.provision.assert_not_awaited()
    device_io.rename.assert_not_awaited()
