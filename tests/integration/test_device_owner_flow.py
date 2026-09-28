"""KSM-TEST-170 (cfoxga/kiosk-satellite-manager#54): the device entry's
Configure -> Enable Device Owner step, driven through HA's real options flow
with the real device_owner module against a scripted ADB shell."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

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


@pytest.fixture(autouse=True)
def meta_start():
    """meta_setup.async_start itself is covered in test_meta_setup.py; here
    only whether and how the flow calls it."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.meta_setup.async_start",
        new=AsyncMock(),
    ) as start:
        yield start


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


async def test_owner_step_explains_and_requires_confirmation(hass, device, meta_start):
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
    # KSM-BEHAVE-112: the purge took the Meta login, so Meta setup follows.
    assert result["reason"] == "device_owner_enabled_meta_setup"
    assert fake.mutations().count(f"dpm set-device-owner {KS_ADMIN}") == 1
    assert fake.owner == "me.jxl.kiosk_satellite" and fake.meta_installed
    meta_start.assert_awaited_once()
    target = meta_start.await_args.args[1]
    assert (target.host, target.port, target.model_key, target.name) == (
        "192.0.2.51", 5555, "portal_mini", "Mini"
    )


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
    assert result["reason"] == "device_owner_enabled_meta_setup"
    assert seen == [True]
    assert coordinator.ksm_installing is False

    coordinator.ksm_installing = True
    patcher, _ = _adb(FakeDevice())
    with patcher:
        result = await _open_owner_step(hass, device)
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "install_in_progress"


async def test_non_meta_model_enrolls_without_meta_setup(hass, meta_start):
    """[KSM-TEST-217] Negative: a model without a Meta setup app ends in the
    plain 'enabled' result and Meta setup is never started."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Gen2", unique_id="192.0.2.52",
        data={
            CONF_HOST: "192.0.2.52", CONF_PORT: 5555, CONF_KEY_PATH: "/k/adbkey",
            CONF_NAME: "Gen2", CONF_DEVICE_PROFILE: "portal_gen2",
        },
    )
    entry.add_to_hass(hass)
    fake = FakeDevice(account_types=())
    patcher, _ = _adb(fake)
    with patcher:
        result = await _open_owner_step(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: True}
        )
    assert result["reason"] == "device_owner_enabled"
    meta_start.assert_not_awaited()


async def test_meta_setup_failure_after_enrollment_is_not_an_enrollment_failure(
    hass, device, meta_start
):
    """[KSM-TEST-217] Negative: Device Owner landed but the setup screen did
    not show -- the result says Device Owner is on and why setup failed."""
    meta_start.side_effect = device_owner.DeviceOwnerError(
        "meta_setup_failed", "the setup screen did not come to the front"
    )
    fake = FakeDevice()
    patcher, _ = _adb(fake)
    with patcher:
        result = await _open_owner_step(hass, device)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: True}
        )
    assert result["reason"] == "device_owner_enabled_meta_failed"
    assert "did not come to the front" in result["description_placeholders"]["reason"]
    assert fake.owner == "me.jxl.kiosk_satellite"


async def test_owner_with_lost_meta_login_offers_meta_setup(hass, device, meta_start):
    """[KSM-TEST-217] Already Device Owner with the Meta login gone: the step
    offers Meta setup, runs nothing unticked, and starts it once ticked --
    the recovery path for a Portal enrolled before KSM-BEHAVE-112."""
    fake = FakeDevice(owner="me.jxl.kiosk_satellite", account_types=("com.facebook.aloha.hw",))
    patcher, _ = _adb(fake)
    with patcher:
        result = await _open_owner_step(hass, device)
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "meta_setup"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: False}
        )
        assert result["errors"] == {CONFIRM: "confirm_required"}
        meta_start.assert_not_awaited()
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: True}
        )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "meta_setup_started"
    meta_start.assert_awaited_once()
    assert fake.mutations() == []


async def test_owner_with_meta_login_is_already_done(hass, device, meta_start):
    """[KSM-TEST-217] Negative: owner and a complete Meta login is still
    'already Device Owner', with no Meta setup offered."""
    patcher, _ = _adb(FakeDevice(owner="me.jxl.kiosk_satellite"))
    with patcher:
        result = await _open_owner_step(hass, device)
    assert result["reason"] == "device_owner_already"
    meta_start.assert_not_awaited()


def _lost_login():
    return FakeDevice(owner="me.jxl.kiosk_satellite", account_types=("com.facebook.aloha.hw",))


@pytest.mark.parametrize("side_effect,text", [
    (device_owner.DeviceOwnerError("meta_setup_failed", "x is still disabled"), "still disabled"),
    (ConnectionResetError("adb closed"), "ADB connection failed"),
])
async def test_meta_setup_step_failure_names_reason(hass, device, meta_start, side_effect, text):
    """[KSM-TEST-217] Negative: a setup screen that could not be shown ends
    in meta_setup_failed with the reason, including a transport drop."""
    meta_start.side_effect = side_effect
    patcher, _ = _adb(_lost_login())
    with patcher:
        result = await _open_owner_step(hass, device)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: True}
        )
    assert result["reason"] == "meta_setup_failed"
    assert text in result["description_placeholders"]["reason"]


async def test_meta_setup_step_holds_lock_and_handles_unreachable_adb(hass, device, meta_start):
    """[KSM-TEST-217] Meta setup holds the entry's install lock while it
    runs, refuses during an install, and aborts cleanly when ADB is gone at
    confirmation."""
    from types import SimpleNamespace

    coordinator = SimpleNamespace(ksm_installing=False, async_update_listeners=lambda: None)
    hass.data.setdefault(DOMAIN, {})[device.entry_id] = coordinator
    seen: list[bool] = []
    meta_start.side_effect = lambda *a: seen.append(coordinator.ksm_installing)
    patcher, _ = _adb(_lost_login())
    with patcher:
        result = await _open_owner_step(hass, device)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: True}
        )
    assert result["reason"] == "meta_setup_started"
    assert seen == [True] and coordinator.ksm_installing is False

    fake = _lost_login()
    patcher, _ = _adb(fake)
    with patcher:
        result = await _open_owner_step(hass, device)
        coordinator.ksm_installing = True
        result = await hass.config_entries.options.async_configure(result["flow_id"], None)
    assert result["reason"] == "install_in_progress"
    coordinator.ksm_installing = False

    connects: list[int] = []

    class Client:
        def __init__(self, *a):
            pass

        async def connect(self, auth_timeout_s: float = 5):
            connects.append(1)
            if len(connects) > 1:
                raise AdbConnectFailed("refused")

        async def close(self):
            pass

        async def shell(self, command):
            return await fake.shell(command)

    with patch("custom_components.kiosk_satellite_manager.config_flow.AdbClient", Client):
        result = await _open_owner_step(hass, device)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONFIRM: True}
        )
    assert result["reason"] == "adb_unavailable"
    assert coordinator.ksm_installing is False
