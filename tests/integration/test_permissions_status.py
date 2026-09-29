"""Permissions status sensor and Fix permissions button (KSM-BEHAVE-141, #102).

The device is a stateful fake at the AdbClient boundary: grants only take
effect for declared permissions, so assertions are on the readback, never on
which commands were sent.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.kiosk_satellite_manager.adb_client import AdbConnectFailed
from custom_components.kiosk_satellite_manager.const import (
    CONF_DEVICE_PROFILE, CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_MANAGER,
)
from custom_components.kiosk_satellite_manager.device_catalog import require_recipe

from .conftest import init_integration

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_CLIENT = "custom_components.kiosk_satellite_manager.permissions.AdbClient"
_MODEL = "onn_4k_pro_android14"
_CAMERA = "android.permission.CAMERA"
_UNDECLARED = "android.permission.POST_NOTIFICATIONS"


class FakeDevice:
    def __init__(self, *, denied=(_CAMERA,), undeclared=(_UNDECLARED,), grants_work=True):
        recipe = require_recipe(_MODEL)
        self.required = recipe.permissions_for_sdk(34)
        self.declared = {p for p in self.required if p not in undeclared}
        self.granted = {p for p in self.declared if p not in denied}
        self.appops = {op: "allow" for op in recipe.appops_for_sdk(34)}
        self.battery = True
        self.grants_work = grants_work
        self.connects = 0
        self.fail_connect = False


def _client_for(device: FakeDevice):
    client = AsyncMock()

    async def connect(*_a, **_k):
        device.connects += 1
        if device.fail_connect:
            raise AdbConnectFailed("down")

    async def shell(cmd):
        parts = cmd.split()
        if parts[:2] == ["pm", "grant"] and device.grants_work and parts[3] in device.declared:
            device.granted.add(parts[3])
        return ""

    client.connect = connect
    client.shell = shell
    client.getprop = AsyncMock(return_value="34")
    client.granted_permissions = AsyncMock(side_effect=lambda: set(device.granted))
    client.declared_permissions = AsyncMock(side_effect=lambda: set(device.declared))
    client.appop_mode = AsyncMock(side_effect=lambda op: device.appops.get(op, "unknown"))
    client.is_battery_exempt = AsyncMock(side_effect=lambda: device.battery)
    client.close = AsyncMock()
    return client


def _entity(hass, entry, suffix):
    ent = next(
        e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id == f"{entry.entry_id}_{suffix}"
    )
    return ent.entity_id


async def _setup(hass, device):
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.92"})), patch(
        _CLIENT, return_value=_client_for(device)
    ):
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: _MODEL})
        await hass.async_block_till_done()
    return ctx


async def test_KSM_TEST_275_declared_but_not_granted_turns_sensor_on(hass):
    device = FakeDevice()
    ctx = await _setup(hass, device)
    state = hass.states.get(_entity(hass, ctx.entry, "permissions"))
    assert state.state == "on"
    assert state.attributes["missing"] == ["CAMERA"]

    clean = FakeDevice(denied=())
    ctx2 = await _setup(hass, clean)
    assert hass.states.get(_entity(hass, ctx2.entry, "permissions")).state == "off"


async def test_KSM_TEST_275_missing_appop_and_battery_exemption_are_reported(hass):
    device = FakeDevice(denied=())
    device.appops["SYSTEM_ALERT_WINDOW"] = "deny"
    device.battery = False
    ctx = await _setup(hass, device)
    state = hass.states.get(_entity(hass, ctx.entry, "permissions"))
    assert state.state == "on"
    assert set(state.attributes["missing"]) == {"appop:SYSTEM_ALERT_WINDOW", "battery_exemption"}


async def test_KSM_TEST_276_undeclared_permission_never_turns_sensor_on(hass):
    device = FakeDevice(denied=())
    ctx = await _setup(hass, device)
    state = hass.states.get(_entity(hass, ctx.entry, "permissions"))
    assert state.state == "off"
    assert state.attributes["missing"] == []
    assert state.attributes["not_declared"] == ["POST_NOTIFICATIONS"]


async def test_KSM_TEST_277_fix_grants_then_sensor_follows_readback(hass):
    device = FakeDevice()
    ctx = await _setup(hass, device)
    sensor = _entity(hass, ctx.entry, "permissions")
    button = _entity(hass, ctx.entry, "fix_permissions")
    assert hass.states.get(sensor).state == "on"
    with patch(_CLIENT, return_value=_client_for(device)):
        await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert _CAMERA in device.granted
    assert hass.states.get(sensor).state == "off"


async def test_KSM_TEST_277_fix_that_does_not_take_stays_on(hass):
    device = FakeDevice(grants_work=False)
    ctx = await _setup(hass, device)
    sensor = _entity(hass, ctx.entry, "permissions")
    with patch(_CLIENT, return_value=_client_for(device)):
        await hass.services.async_call(
            "button", "press", {"entity_id": _entity(hass, ctx.entry, "fix_permissions")}, blocking=True
        )
    state = hass.states.get(sensor)
    assert state.state == "on"
    assert state.attributes["missing"] == ["CAMERA"]


async def test_KSM_TEST_277_unreachable_adb_raises_and_keeps_state(hass):
    device = FakeDevice()
    ctx = await _setup(hass, device)
    sensor = _entity(hass, ctx.entry, "permissions")
    device.fail_connect = True
    with patch(_CLIENT, return_value=_client_for(device)):
        with pytest.raises(HomeAssistantError, match="192.168.99.99"):
            await hass.services.async_call(
                "button", "press",
                {"entity_id": _entity(hass, ctx.entry, "fix_permissions")}, blocking=True,
            )
    assert hass.states.get(sensor).state == "on"


async def test_KSM_TEST_277_unreachable_adb_at_poll_is_unknown_not_problem(hass):
    device = FakeDevice()
    device.fail_connect = True
    ctx = await _setup(hass, device)
    assert hass.states.get(_entity(hass, ctx.entry, "permissions")).state == "unknown"


async def test_KSM_TEST_278_manager_and_unapproved_entries_have_neither_entity(hass):
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.92"})):
        unapproved = await init_integration(hass, data={CONF_DEVICE_PROFILE: "gtv_stick"})
    manager = next(
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER
    )
    for entry in (unapproved.entry, manager):
        ids = {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
        assert f"{entry.entry_id}_permissions" not in ids
        assert f"{entry.entry_id}_fix_permissions" not in ids
