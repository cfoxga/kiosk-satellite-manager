"""KSM-TEST-319..323 (kiosk-satellite-manager#133): the always-on support
record of device-changing ADB runs (KSM-BEHAVE-159/160), driven through the
real AdbClient recording path over a scripted Portal shell, and the HA
"Download diagnostics" file that ships it (KSM-BEHAVE-161)."""
from __future__ import annotations

import asyncio
import json
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigSubentry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import (
    device_owner, diagnostics, meta_setup, support_log,
)
from custom_components.kiosk_satellite_manager.adb_client import AdbClient
from custom_components.kiosk_satellite_manager.const import (
    CONF_DEVICE_PROFILE, CONF_HA_TOKEN, CONF_HOST, CONF_KEY_PATH, CONF_NAME, CONF_PASSWORD,
    CONF_PORT, CONF_TLS_SPKI, DOMAIN,
)
from ksm_device_owner_fake import KS_ADMIN, META, SECRET, FakeDevice

HOST = "192.0.2.51"
KEY = "/k/adbkey"
PASSWORD = "synthetic-ks-password-7f3a"
HA_TOKEN = "synthetic-ha-token-e91c"
PIN = "ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12"
NAME = "Kitchen Portal Of Chris"


class _Transport:
    """Stands in for adb-shell's AdbDeviceTcpAsync under the real AdbClient:
    `/proc/uptime` answers from `uptimes` (an Exception item raises), every
    other command goes to the scripted Portal."""

    def __init__(self, fake, uptimes):
        self.fake = fake
        self.uptimes = list(uptimes)

    async def connect(self, **kwargs):
        pass

    async def close(self):
        pass

    async def shell(self, command, **kwargs):
        if command == "cat /proc/uptime":
            value = self.uptimes.pop(0)
            if isinstance(value, Exception):
                raise value
            return f"{value} 1234.56\n"
        return await self.fake.shell(command)


def _recording_client(fake, uptimes=(5000.25, 5003.75)):
    """A real AdbClient (so its connect/shell recording runs) over _Transport."""

    class Client(AdbClient):
        def __init__(self, host, port, key_path):
            super().__init__(host, port, key_path)
            self._signer = object()
            self._device = _Transport(fake, uptimes)

    return Client


def _dump(hass) -> str:
    return json.dumps(support_log.runs(hass))


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(device_owner, "_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(meta_setup, "WATCH_INTERVAL_S", 0)


@pytest.fixture
def device(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, title=NAME, unique_id=HOST,
        data={
            CONF_HOST: HOST, CONF_PORT: 5555, CONF_KEY_PATH: KEY, CONF_NAME: NAME,
            CONF_DEVICE_PROFILE: "portal_mini", CONF_PASSWORD: PASSWORD,
            CONF_HA_TOKEN: HA_TOKEN, CONF_TLS_SPKI: PIN,
        },
    )
    entry.add_to_hass(hass)
    return entry


# --- KSM-TEST-319: run contents, uptime and reboot detection --------------


@pytest.mark.parametrize("uptimes,rebooted,after", [
    ((5000.25, 5003.75), False, 5003),
    ((5000.25, 12.5), True, 12),
    ((5000.25, ConnectionResetError("gone")), None, None),
])
async def test_run_records_steps_uptime_and_reboot(hass, uptimes, rebooted, after):
    """[KSM-TEST-319] A run records connect uptime, one step per command and
    the end uptime; `rebooted` follows the two readings, and an unreadable
    end reading is unknown, never false."""
    client = _recording_client(FakeDevice(), uptimes)(HOST, 5555, KEY)
    async with support_log.async_run(hass, "device_owner", source="configure",
                                     model_key="portal_mini", client=client):
        await client.connect()
        await client.shell("dumpsys account")
        await client.shell(f"pm uninstall -k --user 0 {META}")
    [run] = support_log.runs(hass)
    assert run["kind"] == "device_owner" and run["source"] == "configure"
    assert run["model"] == "portal_mini" and run["result"] == "ok"
    assert run["uptime_before_s"] == 5000
    assert run["uptime_after_s"] == after
    assert run["rebooted"] is rebooted
    assert [(s.get("cmd"), s.get("out")) for s in run["steps"]] == [
        ("connect", "ok"),
        ("dumpsys account", "output"),
        (f"pm uninstall -k --user 0 {META}", "success"),
    ]
    assert all(isinstance(s["ms"], int) for s in run["steps"])
    assert run["started"].endswith("Z") and isinstance(run["duration_s"], float)
    assert SECRET not in _dump(hass) and HOST not in _dump(hass)


async def test_run_without_connect_has_unknown_reboot(hass):
    """[KSM-TEST-319] No start reading means `rebooted` is unknown."""
    async with support_log.async_run(hass, "meta_setup", model_key="portal_go"):
        support_log.note("meta_setup:shown")
    [run] = support_log.runs(hass)
    assert run["uptime_before_s"] is None and run["rebooted"] is None
    assert run["steps"] == [{"note": "meta_setup:shown"}]


async def test_raising_command_records_class_and_propagates(hass):
    """[KSM-TEST-319] Negative: a command that raises records the exception
    class (never its message) and the run's result; the error still
    reaches the caller unchanged."""
    fake = FakeDevice()
    client = _recording_client(fake)(HOST, 5555, KEY)
    with pytest.raises(AssertionError):
        async with support_log.async_run(hass, "device_owner", source="onboarding",
                                         model_key="portal_mini", client=client):
            await client.connect()
            await client.shell(f"settings put secure x {SECRET}")
    [run] = support_log.runs(hass)
    assert run["result"] == "AssertionError"
    assert run["steps"][-1]["cmd"] == "settings put secure …"
    assert run["steps"][-1]["raised"] == "AssertionError"
    assert SECRET not in _dump(hass)


async def test_device_owner_error_result_is_its_code(hass):
    """[KSM-TEST-319] A DeviceOwnerError ends the run with its code."""
    with pytest.raises(device_owner.DeviceOwnerError):
        async with support_log.async_run(hass, "android9_cleanup", source="configure",
                                         model_key="portal_gen1"):
            raise device_owner.DeviceOwnerError("cleanup_partial", f"could not clear {SECRET}")
    assert support_log.runs(hass)[0]["result"] == "cleanup_partial"


async def test_meta_watch_end_is_recorded(hass):
    """[KSM-TEST-319] The Meta-login watch ending adds a meta_watch run with
    its outcome, even though it runs in a background task."""
    portal = FakeDevice(owner="me.jxl.kiosk_satellite",
                        account_types=tuple(f"com.facebook.aloha.{t}" for t in ("hw", "pl", "privowner", "sso")))

    target = meta_setup.Target(HOST, 5555, KEY, None, None, "portal_go", NAME)
    with patch.object(meta_setup, "AdbClient", _recording_client(portal, (900.0, 901.0, 902.0))), \
            patch.object(meta_setup.persistent_notification, "async_create"):
        meta_setup._start_watch(hass, target, {"started_at": 1e12, "turned_off": []})
        await hass.async_block_till_done(wait_background_tasks=True)
    [run] = support_log.runs(hass)
    assert (run["kind"], run["model"], run["result"]) == ("meta_watch", "portal_go", "login_returned")
    assert run["steps"] == []
    assert HOST not in _dump(hass) and NAME not in _dump(hass)


# --- KSM-TEST-320: bounds, closed runs, persistence ------------------------


async def test_only_newest_runs_and_steps_are_kept(hass):
    """[KSM-TEST-320] 20 runs, 150 steps per run; the rest are counted."""
    for count in range(25):
        async with support_log.async_run(hass, "meta_setup", model_key="portal_go"):
            for _ in range(count):
                support_log.note("meta_setup:shown")
    runs = support_log.runs(hass)
    assert len(runs) == support_log.MAX_RUNS == 20
    assert [len(r["steps"]) for r in runs] == list(range(5, 25))

    async with support_log.async_run(hass, "meta_setup", model_key="portal_go"):
        for _ in range(160):
            support_log.note("meta_setup:shown")
    last = support_log.runs(hass)[-1]
    assert len(last["steps"]) == support_log.MAX_STEPS == 150
    assert last["steps_truncated"] == 10


async def test_closed_run_gains_no_steps(hass):
    """[KSM-TEST-320] Negative: a background task started inside a run
    inherits it, but records nothing once the run has ended."""
    release = asyncio.Event()

    async def late():
        await release.wait()
        support_log.note("meta_setup:shown")
        support_log.record_command("dumpsys account", "output", 3)

    async with support_log.async_run(hass, "device_owner", source="configure", model_key="portal_mini"):
        task = hass.async_create_background_task(late(), "late steps")
    release.set()
    await task
    assert support_log.runs(hass)[0]["steps"] == []


async def test_record_survives_a_store_round_trip(hass, hass_storage):
    """[KSM-TEST-320] The record is saved to KSM's store and reads back the
    same after Home Assistant restarts."""
    async with support_log.async_run(hass, "onboarding_install", model_key="portal_mini"):
        support_log.record_command(f"pm path {KS_ADMIN.split('/')[0]}", "output", 4)
    await hass.async_block_till_done()
    saved = hass_storage[support_log.STORE_KEY]["data"]["runs"]
    assert saved == support_log.runs(hass)
    hass.data.pop(support_log.DATA_KEY)
    assert await support_log.async_load(hass) == saved


# --- KSM-TEST-321: the options Device Owner flow, end to end --------------


async def _owner_flow(hass, device, fake):
    with patch("custom_components.kiosk_satellite_manager.config_flow.AdbClient",
               _recording_client(fake)), \
            patch("custom_components.kiosk_satellite_manager.config_flow.meta_setup.async_start",
                  new=AsyncMock()):
        result = await hass.config_entries.options.async_init(device.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "device_owner"})
        assert result["step_id"] == "device_owner"
        return await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm": True})


async def test_configure_device_owner_is_recorded(hass, device):
    """[KSM-TEST-321] Configure → Enable Device Owner leaves one device_owner
    run with the purge, dpm and restore steps and `ok`; the account name,
    host, key path and device name never appear."""
    fake = FakeDevice()
    result = await _owner_flow(hass, device, fake)
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "device_owner_enabled_meta_setup"
    [run] = support_log.runs(hass)
    assert (run["kind"], run["source"], run["model"], run["result"]) == (
        "device_owner", "configure", "portal_mini", "ok")
    labels = [s.get("cmd") or s.get("note") for s in run["steps"]]
    assert f"pm uninstall -k --user 0 {META}" in labels
    assert ("dpm set-device-owner " + KS_ADMIN) in labels
    assert f"cmd package install-existing --user 0 {META}" in labels
    assert "meta_setup:shown" in labels
    assert run["rebooted"] is False
    dumped = _dump(hass)
    for leaked in (SECRET, HOST, KEY, NAME, PASSWORD, HA_TOKEN, PIN):
        assert leaked not in dumped


async def test_refused_enrollment_records_its_code(hass, device):
    """[KSM-TEST-321] Negative: a refused set-device-owner records the run's
    error code and the refused step's outcome class."""
    fake = FakeDevice(set_owner_output="java.lang.IllegalStateException: refused",
                      set_owner_takes_effect=False)
    result = await _owner_flow(hass, device, fake)
    assert result["reason"] == "device_owner_failed"
    [run] = support_log.runs(hass)
    assert run["result"] == "set_owner_failed"
    dpm = [s for s in run["steps"] if s.get("cmd") == f"dpm set-device-owner {KS_ADMIN}"]
    assert dpm and dpm[0]["out"] == "exception"


# --- KSM-TEST-322: Download diagnostics ------------------------------------


async def test_diagnostics_ship_record_without_secrets(hass, device):
    """[KSM-TEST-322] Diagnostics hold the version, the device's model and
    the record, and none of the entry's credentials or identity."""
    await _owner_flow(hass, device, FakeDevice())
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, device)
    assert diag["entry_type"] == "device"
    assert diag["devices"] == [
        {"model": "portal_mini", "meta_watch_pending": False, "support_request": None}
    ]
    assert diag["support_log"] == support_log.runs(hass) and diag["support_log"]
    assert diag["version"]
    dumped = json.dumps(diag)
    for leaked in (PASSWORD, HA_TOKEN, PIN, KEY, HOST, NAME, SECRET, device.entry_id):
        assert leaked not in dumped


async def test_diagnostics_of_a_fleet_entry(hass):
    """[KSM-TEST-322] A fleet parent lists each subentry device's model and
    pending watch only; an unknown model is `unlisted`."""
    parent = MockConfigEntry(domain=DOMAIN, title="Unmanaged", data={"entry_type": "unmanaged"})
    parent.add_to_hass(hass)
    for sub_id, model, pending in (("a", "portal_go", {"started_at": 1.0, "turned_off": []}),
                                   ("b", "Chris's own tablet", None)):
        data = {CONF_HOST: f"192.0.2.6{len(sub_id)}", CONF_PORT: 5555, CONF_KEY_PATH: KEY,
                CONF_PASSWORD: PASSWORD, CONF_DEVICE_PROFILE: model, CONF_NAME: NAME}
        if pending:
            data[meta_setup.PENDING_KEY] = pending
        hass.config_entries.async_add_subentry(parent, ConfigSubentry(
            data=MappingProxyType(data), subentry_id=sub_id, subentry_type="device",
            title=NAME, unique_id=sub_id,
        ))
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, parent)
    assert diag["entry_type"] == "unmanaged"
    assert sorted(diag["devices"], key=lambda d: d["model"]) == [
        {"model": "portal_go", "meta_watch_pending": True, "support_request": None},
        {"model": "unlisted", "meta_watch_pending": False, "support_request": None},
    ]
    dumped = json.dumps(diag)
    for leaked in (PASSWORD, KEY, NAME, "192.0.2.6", "Chris's own tablet"):
        assert leaked not in dumped


# --- KSM-TEST-323: the Meta-setup notification wording ---------------------


async def test_meta_setup_notice_says_no_reboot(hass):
    """[KSM-TEST-323] The notification shown with Meta's setup screen says
    the Portal did not reboot or reset."""
    target = meta_setup.Target(HOST, 5555, KEY, None, None, "portal_go", NAME)
    with patch.object(meta_setup, "kiosk_lock_off", AsyncMock(return_value=())), \
            patch.object(meta_setup.device_owner, "restart_meta_setup", AsyncMock()), \
            patch.object(meta_setup, "_start_watch"), \
            patch.object(meta_setup.persistent_notification, "async_create") as notice:
        await meta_setup.async_start(hass, target, object())
    assert "did not reboot or reset" in notice.call_args.kwargs["message"]
