"""KSM-TEST-341/343/344 (#140): Uninstall on a Device Owner device, its
factory-reset repair and the repair's fix flow.

Only the ADB transport and Kiosk Satellite's settings API are faked: the
button, issue registry, repairs flow manager and device_owner logic are real.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest

from custom_components.kiosk_satellite_manager import device_owner
from custom_components.kiosk_satellite_manager.adb_client import UninstallDeviceOwner
from custom_components.kiosk_satellite_manager.const import CONF_DEVICE_PROFILE, DOMAIN

from ksm_device_owner_fake import KS_ACTIVITY, RESET_ACTION, RESET_ACTIVITY, FakeDevice

from .conftest import init_integration

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_BUTTON_ADB = "custom_components.kiosk_satellite_manager.button.AdbClient"
_REPAIR_ADB = "custom_components.kiosk_satellite_manager.repairs.AdbClient"
_LOCK_OFF = "custom_components.kiosk_satellite_manager.meta_setup.kiosk_lock_off"
_LOCK_ON = "custom_components.kiosk_satellite_manager.meta_setup.restore_kiosk_lock"


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(device_owner, "_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(device_owner, "_POLL_ATTEMPTS", 3)


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"factory_reset_{entry.entry_id}")


async def _setup(hass, profile="portal_gen1"):
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.91"})):
        return (await init_integration(hass, data={CONF_DEVICE_PROFILE: profile})).entry


async def _press_uninstall(hass, entry, uninstall: AsyncMock):
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_uninstall"
    )
    with patch(_BUTTON_ADB) as client_cls:
        client = client_cls.return_value
        client.connect = AsyncMock()
        client.uninstall_ks = uninstall
        client.close = AsyncMock()
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
    return client


# --- KSM-TEST-341: the button ----------------------------------------------

async def test_KSM_TEST_341_device_owner_explains_and_raises_a_fixable_repair(hass):
    entry = await _setup(hass)
    with pytest.raises(HomeAssistantError, match=r"Device Owner.*factory reset") as err:
        await _press_uninstall(hass, entry, AsyncMock(side_effect=UninstallDeviceOwner()))
    assert "DELETE_FAILED" not in str(err.value)
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_key == "factory_reset"
    assert issue.data == {"entry_id": entry.entry_id}


async def test_KSM_TEST_341_an_unverified_model_gets_an_explanation_only(hass):
    entry = await _setup(hass, profile="portal_go")
    with pytest.raises(HomeAssistantError, match="Device Owner"):
        await _press_uninstall(hass, entry, AsyncMock(side_effect=UninstallDeviceOwner()))
    issue = _issue(hass, entry)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.translation_key == "factory_reset_manual"


async def test_KSM_TEST_341_a_successful_uninstall_clears_the_repair(hass):
    """Negative: no Device Owner, no repair -- and an old one goes away."""
    entry = await _setup(hass)
    with pytest.raises(HomeAssistantError):
        await _press_uninstall(hass, entry, AsyncMock(side_effect=UninstallDeviceOwner()))
    assert _issue(hass, entry) is not None
    await _press_uninstall(hass, entry, AsyncMock())
    assert _issue(hass, entry) is None


# --- KSM-TEST-343/344: the fix flow -----------------------------------------

async def _start_flow(hass, entry):
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    result = await manager.async_init(
        DOMAIN, data={"issue_id": f"factory_reset_{entry.entry_id}"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    return manager, result


async def _raise_repair(hass, entry):
    with pytest.raises(HomeAssistantError):
        await _press_uninstall(hass, entry, AsyncMock(side_effect=UninstallDeviceOwner()))


def _repair_adb(dev: FakeDevice | None, *, connect_error: Exception | None = None):
    patcher = patch(_REPAIR_ADB)
    client = patcher.start().return_value
    client.connect = AsyncMock(side_effect=connect_error)
    client.shell = AsyncMock(side_effect=dev.shell if dev else None)
    client.close = AsyncMock()
    return patcher, client


async def test_KSM_TEST_343_confirm_turns_the_lock_off_and_opens_the_screen(hass):
    entry = await _setup(hass)
    await _raise_repair(hass, entry)
    manager, result = await _start_flow(hass, entry)
    assert result["description_placeholders"]["name"] == entry.title
    dev = FakeDevice(owner="me.jxl.kiosk_satellite")
    patcher, client = _repair_adb(dev)
    try:
        with patch(_LOCK_OFF, new=AsyncMock(return_value=("kiosk.enabled",))) as lock_off, \
                patch(_LOCK_ON, new=AsyncMock(return_value=True)) as lock_on:
            result = await manager.async_configure(result["flow_id"], {})
            await hass.async_block_till_done()
    finally:
        patcher.stop()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    lock_off.assert_awaited_once()
    lock_on.assert_not_awaited()  # putting the lock back would hide the screen
    assert dev.front == RESET_ACTIVITY
    client.close.assert_awaited_once()
    # [KSM-TEST-344] KSM opens the page and reads what is in front -- nothing else.
    assert dev.commands[0] == f"am start -a {RESET_ACTION}"
    assert set(dev.commands[1:]) == {"dumpsys activity activities"}
    assert not [c for c in dev.commands
                if c.startswith(("input", "am broadcast", "dpm", "cmd testharness"))]


async def test_KSM_TEST_343_adb_unreachable_aborts_and_keeps_the_repair(hass):
    entry = await _setup(hass)
    await _raise_repair(hass, entry)
    manager, result = await _start_flow(hass, entry)
    patcher, client = _repair_adb(None, connect_error=OSError("refused"))
    try:
        with patch(_LOCK_OFF, new=AsyncMock()) as lock_off:
            result = await manager.async_configure(result["flow_id"], {})
    finally:
        patcher.stop()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect_adb"
    lock_off.assert_not_awaited()
    assert _issue(hass, entry) is not None
    client.close.assert_awaited_once()


async def test_KSM_TEST_343_a_hidden_screen_aborts_and_restores_the_lock(hass):
    entry = await _setup(hass)
    await _raise_repair(hass, entry)
    manager, result = await _start_flow(hass, entry)
    dev = FakeDevice(owner="me.jxl.kiosk_satellite", reset_screen_launches=False)
    patcher, _ = _repair_adb(dev)
    try:
        with patch(_LOCK_OFF, new=AsyncMock(return_value=("kiosk.enabled",))), \
                patch(_LOCK_ON, new=AsyncMock(return_value=True)) as lock_on:
            result = await manager.async_configure(result["flow_id"], {})
    finally:
        patcher.stop()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "factory_reset_failed"
    lock_on.assert_awaited_once()
    assert lock_on.await_args.args[2] == ("kiosk.enabled",)
    assert dev.front == KS_ACTIVITY
    assert _issue(hass, entry) is not None
