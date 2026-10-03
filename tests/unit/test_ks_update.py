"""Unit tests for the self-update sequence (KSM-BEHAVE-082, #47) that exercise
branches the integration suite's happy-path/ATTEMPTS=1 shape doesn't reach:
no coordinator registered for the entry, no usable release, a transient
failure mid-install, and the multi-attempt poll loop (including its
between-attempts sleep). No real hass fixture -- hass/entry are lightweight
fakes, since async_self_update_entry only reads hass.data and entry.data/title.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.kiosk_satellite_manager import ks_update
from custom_components.kiosk_satellite_manager.const import (
    CONF_DEVICE_PROFILE,
    CONF_HOST,
    CONF_KEY_PATH,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
    RELEASE_COORDINATOR_KEY,
)
from custom_components.kiosk_satellite_manager.ks_api import ApkAssetNotFound
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

_SESSION = "custom_components.kiosk_satellite_manager.ks_update.async_get_clientsession"
_LOGIN = "custom_components.kiosk_satellite_manager.ks_update.ks_api_client.login"
_RUN_COMMAND = "custom_components.kiosk_satellite_manager.ks_update.ks_api_client.run_command"
_POLL_HEALTH = "custom_components.kiosk_satellite_manager.ks_update.fetch_health"


def _entry(**data):
    return SimpleNamespace(
        data={CONF_HOST: "192.168.1.50", CONF_PASSWORD: "secret", **data},
        title="Test Device",
        entry_id="entry-1",
    )


def _hass(release_version="2026.9.77", coordinator=None):
    data = {RELEASE_COORDINATOR_KEY: SimpleNamespace(data=SimpleNamespace(version=release_version))}
    if coordinator is not None:
        data[DOMAIN] = {"entry-1": coordinator}

    async def async_add_executor_job(func, *args):
        return await asyncio.to_thread(func, *args)

    return SimpleNamespace(data=data, async_add_executor_job=async_add_executor_job)


@pytest.fixture(autouse=True)
def cached_apk(tmp_path):
    """KSM-BEHAVE-107/108: a real small cached file, an accepting upload."""
    apk = tmp_path / "ks.apk"
    apk.write_bytes(b"apk")

    async def upload(session, host, token, body, size, *, pin=None):
        async for _ in body:
            pass
        return {"ok": True, "data": {"buildNumber": 2, "currentBuild": 1}}

    prefix = "custom_components.kiosk_satellite_manager.ks_update."
    with patch(prefix + "apk_cache.async_release_apk", new=AsyncMock(return_value=apk)), patch(
        prefix + "apk_cache.async_prune", new=AsyncMock(return_value=[])
    ), patch(prefix + "ks_api_client.upload_update", new=AsyncMock(side_effect=upload)):
        yield apk


def _commands(**by_command):
    async def fake_run_command(session, host, token, command, *, pin=None):
        value = by_command[command]
        if command == "getUpdateStatus":
            return {"ok": True, "data": value}
        return value
    return fake_run_command


async def test_no_usable_release_raises():
    hass = _hass()
    hass.data[RELEASE_COORDINATOR_KEY] = SimpleNamespace(data=None)
    with pytest.raises(HomeAssistantError, match="No usable"):
        await ks_update.async_self_update_entry(hass, _entry())


@pytest.mark.parametrize("reinstall", [False, True])
async def test_KSM_TEST_349_same_build_only_reinstalls_on_explicit_press(cached_apk, reinstall):
    prefix = "custom_components.kiosk_satellite_manager.ks_update."
    with patch(_SESSION), patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
        prefix + "ks_api_client.upload_update", new=AsyncMock(return_value={
            "ok": True, "data": {"buildNumber": 2, "currentBuild": 2}
        })
    ), patch(_RUN_COMMAND, new=AsyncMock(return_value={
        "ok": True, "data": {"lastOutcome": "silent", "installing": False}
    })) as command, patch(
        _POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.77"})
    ):
        assert await ks_update.async_self_update_entry(_hass(), _entry(), reinstall=reinstall) == ks_update.OUTCOME_UPDATED
    installs = [call for call in command.await_args_list if call.args[3] == "installUploadedApk"]
    assert len(installs) == int(reinstall)


async def test_KSM_TEST_349_login_connection_failure_allows_adb_recovery():
    connection = aiohttp.ClientConnectorError(None, OSError("refused"))
    with patch(_SESSION), patch(_LOGIN, new=AsyncMock(side_effect=connection)), patch(
        "custom_components.kiosk_satellite_manager.ks_update.apk_cache.async_release_apk",
        new=AsyncMock(),
    ) as apk:
        with pytest.raises(ks_update.ApiInstallUnavailable, match="API is unreachable"):
            await ks_update.async_self_update_entry(_hass(), _entry(), reinstall=True)
    apk.assert_not_awaited()


async def test_KSM_TEST_349_login_rejection_is_not_adb_recovery():
    with patch(_SESSION), patch(_LOGIN, new=AsyncMock(side_effect=KsApiError("bad password"))):
        with pytest.raises(HomeAssistantError, match="bad password") as error:
            await ks_update.async_self_update_entry(_hass(), _entry(), reinstall=True)
    assert not isinstance(error.value, ks_update.ApiInstallUnavailable)


async def test_KSM_TEST_349_tls_pin_mismatch_is_not_adb_recovery():
    mismatch = aiohttp.ServerFingerprintMismatch(b"a", b"b", "device", 2324)
    with patch(_SESSION), patch(_LOGIN, new=AsyncMock(side_effect=mismatch)):
        with pytest.raises(HomeAssistantError) as error:
            await ks_update.async_self_update_entry(_hass(), _entry(), reinstall=True)
    assert not isinstance(error.value, ks_update.ApiInstallUnavailable)


async def test_KSM_TEST_349_same_build_health_does_not_hide_confirmation():
    with patch(_POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.77"})), patch(
        _RUN_COMMAND, new=AsyncMock(return_value={
            "ok": True, "data": {"lastOutcome": "confirm", "installing": False}
        })
    ), patch("custom_components.kiosk_satellite_manager.ks_update.SELF_UPDATE_POLL_ATTEMPTS", 1):
        assert await ks_update._poll_until_resolved(
            None, "192.168.1.50", "tok", "2026.9.77", _entry(), pin=None,
            same_build_reinstall=True,
        ) == ks_update.OUTCOME_AWAITING_CONFIRMATION


async def test_portal_verifier_failure_remediates_and_retries_once(cached_apk):
    """[KSM-TEST-229] Only a confirmed Portal verifier rejection opens ADB.
    The prior value is read and the changed value is verified before retry."""
    entry = _entry(**{CONF_DEVICE_PROFILE: "portal_gen2", CONF_PORT: 5555,
                      CONF_KEY_PATH: "/tmp/test-adb-key"})
    client = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(),
                             shell=AsyncMock(side_effect=["1", "", "0"]))
    commands = []

    async def command(session, host, token, name, *, pin=None):
        commands.append(name)
        if name == "getDeviceInfo":
            return {"ok": True, "data": {"abis": ["arm64-v8a"]}}
        if name == "installUploadedApk":
            return {"ok": True}
        return {"ok": True, "data": {
            "lastError": "INSTALL_FAILED_VERIFICATION_FAILURE"
            if commands.count("getUpdateStatus") == 1 else None}}

    with patch(_SESSION), patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
        _RUN_COMMAND, new=command
    ), patch(_POLL_HEALTH, new=AsyncMock(side_effect=[{"appVersion": "old"},
                                                        {"appVersion": "2026.9.77"}])), patch(
        "custom_components.kiosk_satellite_manager.ks_update.AdbClient", return_value=client
    ) as adb:
        assert await ks_update.async_self_update_entry(_hass(), entry) == ks_update.OUTCOME_UPDATED

    adb.assert_called_once_with("192.168.1.50", 5555, "/tmp/test-adb-key")
    assert [call.args[0] for call in client.shell.await_args_list] == [
        "settings get global package_verifier_enable",
        "settings put global package_verifier_enable 0",
        "settings get global package_verifier_enable",
    ]
    assert commands.count("installUploadedApk") == 2
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("profile", "error"),
    [(None, "INSTALL_FAILED_VERIFICATION_FAILURE"),
     ("other_model", "INSTALL_FAILED_VERIFICATION_FAILURE"),
     ("portal_gen2", "INSTALL_FAILED_INSUFFICIENT_STORAGE")],
)
async def test_other_update_failures_never_use_adb(cached_apk, profile, error):
    """[KSM-TEST-229] Non-Portal or unrelated failures retain API-only behavior."""
    entry = _entry(**({CONF_DEVICE_PROFILE: profile} if profile else {}))
    with patch(_SESSION), patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
        _RUN_COMMAND, new=_commands(installUploadedApk={"ok": True},
                                    getUpdateStatus={"lastError": error})
    ), patch(_POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "old"})), patch(
        "custom_components.kiosk_satellite_manager.ks_update.AdbClient"
    ) as adb, patch(
        "custom_components.kiosk_satellite_manager.ks_update.require_recipe",
        return_value=SimpleNamespace(verifier_retry_on_failure=False)
    ) if profile == "other_model" else patch(
        "custom_components.kiosk_satellite_manager.ks_update.require_recipe",
        wraps=ks_update.require_recipe
    ):
        with pytest.raises(HomeAssistantError, match=error):
            await ks_update.async_self_update_entry(_hass(), entry)
    adb.assert_not_called()


async def test_portal_verifier_readback_must_confirm_disable():
    """[KSM-TEST-229] An ADB write reply alone is not recovery evidence."""
    entry = _entry(**{CONF_PORT: 5555, CONF_KEY_PATH: "/tmp/test-adb-key"})
    client = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(),
                             shell=AsyncMock(side_effect=["1", "", "1"]))
    with patch("custom_components.kiosk_satellite_manager.ks_update.AdbClient",
               return_value=client):
        with pytest.raises(HomeAssistantError, match="did not disable"):
            await ks_update._remediate_verifier(entry)
    client.close.assert_awaited_once()


async def test_no_coordinator_registered_still_completes_and_skips_the_refresh():
    """[KSM-TEST-153] A device entry with no health coordinator yet (e.g. the
    manager's Update all racing setup) updates over the API the same way,
    it just has nothing to flag installing or refresh afterward."""
    hass = _hass()
    with patch(_SESSION), patch(_POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.77"})), patch(
        _LOGIN, new=AsyncMock(return_value="device-token")
    ), patch(
        _RUN_COMMAND,
        new=_commands(
            getUpdateStatus={},
            installUploadedApk={"ok": True},
        ),
    ):
        outcome = await ks_update.async_self_update_entry(hass, _entry())
    assert outcome == ks_update.OUTCOME_UPDATED


async def test_install_update_transient_failure_is_reported_as_failed():
    """[KSM-TEST-156] A transient error on installUploadedApk itself (not
    just login/upload) fails loudly, naming the entry."""
    hass = _hass()

    async def fake_run_command(session, host, token, command, *, pin=None):
        if command == "installUploadedApk":
            raise KsApiError("connection reset")
        raise AssertionError(f"unexpected command: {command}")

    with patch(_SESSION), patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
        _RUN_COMMAND, new=fake_run_command
    ):
        with pytest.raises(HomeAssistantError, match="update failed on Test Device"):
            await ks_update.async_self_update_entry(hass, _entry())


async def test_poll_treats_a_transient_health_error_as_not_yet_healthy():
    """[KSM-TEST-156] A connection-refused /api/health read during the
    restart window is swallowed, not raised -- the poll just keeps going."""
    with patch(
        _POLL_HEALTH,
        new=AsyncMock(
            side_effect=[aiohttp.ClientError("connection refused"), {"appVersion": "2026.9.77"}]
        ),
    ), patch(_RUN_COMMAND, new=_commands(getUpdateStatus={"lastOutcome": "confirm"})), patch(
        "custom_components.kiosk_satellite_manager.ks_update.SELF_UPDATE_POLL_ATTEMPTS", 2
    ), patch(
        "custom_components.kiosk_satellite_manager.ks_update.SELF_UPDATE_POLL_DELAY_S", 0
    ):
        outcome = await ks_update._poll_until_resolved(
            session=None, host="192.168.1.50", token="tok", version="2026.9.77", entry=_entry(), pin=None,
        )
    assert outcome == ks_update.OUTCOME_UPDATED


async def test_poll_treats_a_transient_status_error_as_no_outcome_yet():
    """[KSM-TEST-156] getUpdateStatus itself failing transiently during the
    poll doesn't end the poll -- it's the same "still restarting" tolerance
    as the health read, just on the other call the poll makes each attempt."""
    async def flaky_status(session, host, token, command, *, pin=None):
        raise KsApiError("temporarily unreachable")

    with patch(_POLL_HEALTH, new=AsyncMock(return_value=None)), patch(
        _RUN_COMMAND, new=flaky_status
    ), patch(
        "custom_components.kiosk_satellite_manager.ks_update.SELF_UPDATE_POLL_ATTEMPTS", 1
    ):
        with pytest.raises(HomeAssistantError, match="did not complete"):
            await ks_update._poll_until_resolved(
                session=None, host="192.168.1.50", token="tok", version="2026.9.77", entry=_entry(), pin=None,
            )


def _fanout_hass(entries, coordinators):
    return SimpleNamespace(
        data={DOMAIN: coordinators},
        config_entries=SimpleNamespace(async_get_entry=lambda entry_id: entries.get(entry_id)),
    )


async def test_check_devices_for_update_asks_each_device_and_skips_the_rest():
    """[KSM-TEST-197] KSM-BEHAVE-103: checkUpdateNow to every loaded device with
    a password; one failure never stops the next; no-password and installing
    devices are never logged into."""
    def entry(entry_id, title, host, **extra):
        data = {CONF_HOST: host, CONF_PASSWORD: "secret", **extra}
        return SimpleNamespace(entry_id=entry_id, title=title, data=data, domain=DOMAIN)

    entries = {
        "a": entry("a", "Failing", "192.168.1.1"),
        "b": entry("b", "Kitchen", "192.168.1.2"),
        "c": entry("c", "No password", "192.168.1.3", **{CONF_PASSWORD: ""}),
        "d": entry("d", "Busy", "192.168.1.4"),
        "e": entry("e", "Rejecting", "192.168.1.5"),
    }
    coordinators = {
        entry_id: SimpleNamespace(ksm_installing=entry_id == "d") for entry_id in entries
    }

    async def fake_login(session, host, password, *, pin=None):
        if host == "192.168.1.1":
            raise KsApiError("HTTP 401")
        return f"token-{host}"

    login = AsyncMock(side_effect=fake_login)
    async def fake_run(session, host, token, command, *, pin=None):
        if host == "192.168.1.5":
            return {"ok": False, "error": "update check disabled"}
        return {"ok": True, "data": {"reachable": True, "availableVersion": "2026.9.2"}}

    run = AsyncMock(side_effect=fake_run)
    with patch(_SESSION), patch(_LOGIN, new=login), patch(_RUN_COMMAND, new=run):
        results = await ks_update.async_check_devices_for_update(_fanout_hass(entries, coordinators))

    assert sorted(call.args[1] for call in login.await_args_list) == [
        "192.168.1.1", "192.168.1.2", "192.168.1.5",
    ]
    assert sorted(call.args[1:4] for call in run.await_args_list) == [
        ("192.168.1.2", "token-192.168.1.2", "checkUpdateNow"),
        ("192.168.1.5", "token-192.168.1.5", "checkUpdateNow"),
    ]
    assert results["Rejecting"] == "failed: update check disabled"
    assert results["Kitchen"] == "sees 2026.9.2"
    assert results["Failing"].startswith("failed") and "401" in results["Failing"]
    assert results["No password"] == "skipped: no password stored"
    assert results["Busy"] == "skipped: install in progress"


async def test_apk_unavailable_fails_before_any_upload(cached_apk):
    """[KSM-TEST-209] no usable asset (or a download/signer failure) is a
    failed update naming the entry; nothing is uploaded or installed."""
    hass = _hass()
    sent = []

    async def fake_run_command(session, host, token, command, *, pin=None):
        sent.append(command)
        if command == "getDeviceInfo":
            return {"ok": True, "data": {"abis": ["arm64-v8a"]}}
        raise AssertionError(f"unexpected command: {command}")

    prefix = "custom_components.kiosk_satellite_manager.ks_update."
    with patch(_SESSION), patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
        _RUN_COMMAND, new=fake_run_command
    ), patch(
        prefix + "apk_cache.async_release_apk",
        new=AsyncMock(side_effect=ApkAssetNotFound("no APK for the device")),
    ), patch(prefix + "ks_api_client.upload_update", new=AsyncMock()) as upload:
        with pytest.raises(HomeAssistantError, match="APK unavailable for Test Device"):
            await ks_update.async_self_update_entry(hass, _entry())
    upload.assert_not_awaited()
    assert sent == ["getDeviceInfo"]


@pytest.mark.parametrize(
    ("device_info", "abis"),
    [
        ({"ok": True, "data": {"abis": ["armeabi-v7a", "armeabi", 7]}}, ["armeabi-v7a", "armeabi"]),
        ({"ok": False, "error": "unknown command"}, []),
        (KsApiError("timeout"), []),
    ],
)
async def test_self_update_asks_the_device_for_its_abis(cached_apk, device_info, abis):
    """[KSM-TEST-224] #74: the self-update gets the APK for the ABIs Kiosk
    Satellite's getDeviceInfo reports. Negative cases: a refused or failed
    getDeviceInfo means "unknown" (universal fallback), never a failed update."""
    hass = _hass()

    async def fake_run_command(session, host, token, command, *, pin=None):
        if command == "getDeviceInfo":
            if isinstance(device_info, Exception):
                raise device_info
            return device_info
        if command == "getUpdateStatus":
            return {"ok": True, "data": {}}
        return {"ok": True}

    prefix = "custom_components.kiosk_satellite_manager.ks_update."
    with patch(_SESSION), patch(_POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.77"})), patch(
        _LOGIN, new=AsyncMock(return_value="device-token")
    ), patch(_RUN_COMMAND, new=fake_run_command), patch(
        prefix + "apk_cache.async_release_apk", new=AsyncMock(return_value=cached_apk)
    ) as release_apk:
        assert await ks_update.async_self_update_entry(hass, _entry()) == ks_update.OUTCOME_UPDATED
    assert release_apk.await_args.args[2] == abis
