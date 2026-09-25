"""KSM-TEST-170 (cfoxga/kiosk-satellite-manager#54): the device entry's
Configure -> Enable Device Owner step, driven through HA's real options flow
with the real device_owner module against a scripted ADB shell."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import device_owner
from custom_components.kiosk_satellite_manager.adb_client import AdbConnectFailed
from custom_components.kiosk_satellite_manager.const import (
    CONF_DEVICE_PROFILE, CONF_HOST, CONF_KEY_PATH, CONF_NAME, CONF_PORT, DOMAIN,
)
from ksm_device_owner_fake import FakeDevice, KS_ADMIN, META, SECRET

CONFIRM = "confirm"


@pytest.fixture
def device(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Mini", unique_id="192.0.2.51",
        data={
            CONF_HOST: "192.0.2.51", CONF_PORT: 5555, CONF_KEY_PATH: "/k/adbkey",
            CONF_NAME: "Mini", CONF_DEVICE_PROFILE: "portal_mini",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(device_owner, "_POLL_INTERVAL_S", 0)


def _adb(fake: FakeDevice | None, connect_error: Exception | None = None):
    """Patch config_flow's AdbClient with a client wrapping `fake`."""
    made: list[tuple] = []

    class Client:
        def __init__(self, host, port, key_path):
            made.append((host, port, key_path))

        async def connect(self, auth_timeout_s: float = 5):
            if connect_error:
                raise connect_error

        async def close(self):
            pass

        async def shell(self, command):
            return await fake.shell(command)

    return patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient", Client
    ), made


async def _open_owner_step(hass, device):
    result = await hass.config_entries.options.async_init(device.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert set(result["menu_options"]) == {"device_password", "device_owner"}
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device_owner"}
    )


async def test_owner_step_explains_and_requires_confirmation(hass, device):
    """[KSM-TEST-170] The step shows the account summary (types, no names);
    unticked runs nothing; ticked enrolls once and ends enabled."""
    fake = FakeDevice()
    patcher, made = _adb(fake)
    with patcher:
        result = await _open_owner_step(hass, device)
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "device_owner"
        summary = result["description_placeholders"]["accounts"]
        assert "com.facebook.aloha.sso" in summary and META in summary
        assert SECRET not in str(result)
        assert made[0] == ("192.0.2.51", 5555, "/k/adbkey")
        assert fake.mutations() == []

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: False}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["errors"] == {CONFIRM: "confirm_required"}
        assert fake.mutations() == []

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: True}
        )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "device_owner_enabled"
    assert fake.mutations().count(f"dpm set-device-owner {KS_ADMIN}") == 1
    assert fake.owner == "me.jxl.kiosk_satellite" and fake.meta_installed


@pytest.mark.parametrize("kwargs,reason", [
    ({"owner": "me.jxl.kiosk_satellite"}, "device_owner_already"),
    ({"owner": "com.example.dpc"}, "device_owner_blocked"),
    ({"account_types": ("com.example.other",)}, "device_owner_blocked"),
    ({"users": 2}, "device_owner_blocked"),
])
async def test_owner_step_aborts_on_blocker(hass, device, kwargs, reason):
    """[KSM-TEST-170] Negative: preflight blockers abort with their reason
    before any form, and the device is untouched."""
    fake = FakeDevice(**kwargs)
    patcher, _ = _adb(fake)
    with patcher:
        result = await _open_owner_step(hass, device)
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == reason
    if reason == "device_owner_blocked":
        assert result["description_placeholders"]["reason"]
    assert fake.mutations() == []


async def test_owner_step_aborts_when_adb_unreachable(hass, device):
    """[KSM-TEST-170] Negative: refused ADB aborts naming ADB, no retries
    against a device that isn't there."""
    patcher, _ = _adb(None, AdbConnectFailed("refused"))
    with patcher:
        result = await _open_owner_step(hass, device)
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "adb_unavailable"
    assert "192.0.2.51:5555" in result["description_placeholders"]["address"]


async def test_owner_failure_aborts_with_reason_and_restores(hass, device):
    """[KSM-TEST-170] Negative: a failed set-device-owner ends in
    device_owner_failed, never enabled, and the package is restored."""
    fake = FakeDevice(set_owner_output="IllegalStateException", set_owner_takes_effect=False)
    patcher, _ = _adb(fake)
    with patcher:
        result = await _open_owner_step(hass, device)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: True}
        )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "device_owner_failed"
    assert fake.meta_installed is True
    assert SECRET not in str(result)


async def test_manager_options_are_not_a_menu(hass):
    """[KSM-TEST-170] Negative: the manager entry keeps its settings form."""
    manager = MockConfigEntry(domain=DOMAIN, title="KSM", data={"entry_type": "manager"})
    manager.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(manager.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"


async def test_enrollment_holds_the_entry_install_lock(hass, device):
    """[KSM-TEST-170] Negative (review): an install cannot start on this entry
    while enrollment runs -- the step raises ksm_installing for its duration."""
    from types import SimpleNamespace

    coordinator = SimpleNamespace(ksm_installing=False, async_update_listeners=lambda: None)
    hass.data.setdefault(DOMAIN, {})[device.entry_id] = coordinator
    seen: list[bool] = []

    class Watch(FakeDevice):
        async def shell(self, command: str) -> str:
            if command.startswith("dpm set"):
                seen.append(coordinator.ksm_installing)
            return await super().shell(command)

    fake = Watch()
    patcher, _ = _adb(fake)
    with patcher:
        result = await _open_owner_step(hass, device)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: True}
        )
    assert result["reason"] == "device_owner_enabled"
    assert seen == [True]
    assert coordinator.ksm_installing is False

    coordinator.ksm_installing = True
    patcher, _ = _adb(FakeDevice())
    with patcher:
        result = await _open_owner_step(hass, device)
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "install_in_progress"
