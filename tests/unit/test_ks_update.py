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
    CONF_HOST,
    CONF_PASSWORD,
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
        return SimpleNamespace(entry_id=entry_id, title=title, data=data)

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
        raise AssertionError(f"unexpected command: {command}")

    prefix = "custom_components.kiosk_satellite_manager.ks_update."
    with patch(_SESSION), patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
        _RUN_COMMAND, new=fake_run_command
    ), patch(
        prefix + "apk_cache.async_release_apk",
        new=AsyncMock(side_effect=ApkAssetNotFound("no universal APK")),
    ), patch(prefix + "ks_api_client.upload_update", new=AsyncMock()) as upload:
        with pytest.raises(HomeAssistantError, match="APK unavailable for Test Device"):
            await ks_update.async_self_update_entry(hass, _entry())
    upload.assert_not_awaited()
    assert sent == []
