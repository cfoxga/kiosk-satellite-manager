"""Shared release check, the KS self-update install path, and opt-in
auto-update (KSM-BEHAVE-071/073/082/134, issues #43, #47, #95). KSM adds no
update entity or version sensor (KSM-BEHAVE-134). The GitHub
release check and the device's /api/health are mocked at the boundary (see
conftest's `release_check`).

*Superseded 2026-09-24 (#47)*: Install/auto-update/Update all no longer go
through the button's ADB install sequence -- they run Kiosk Satellite's own
self-update over its `:2324` API (`ks_update.async_self_update_entry`).
`AdbClient` is patched to fail on construction in every install test below
and asserted never constructed.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager.const import (
    CONF_AUTO_UPDATE,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_PASSWORD,
    DOMAIN,
    ENTRY_TYPE_MANAGER,
    RELEASE_COORDINATOR_KEY,
)
from custom_components.kiosk_satellite_manager.ks_api import ReleaseInfo
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

from custom_components.kiosk_satellite_manager.ks_update import (
    OUTCOME_AWAITING_CONFIRMATION,
    async_self_update_entry,
)

from .conftest import init_integration

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_AUTO_INSTALL = "custom_components.kiosk_satellite_manager.auto_update.async_self_update_entry"
_ADB_CLIENT = "custom_components.kiosk_satellite_manager.button.AdbClient"
_LOGIN = "custom_components.kiosk_satellite_manager.ks_update.ks_api_client.login"
_RUN_COMMAND = "custom_components.kiosk_satellite_manager.ks_update.ks_api_client.run_command"
# ks_update.py binds its own `fetch_health` name (`from .provisioning import
# fetch_health`) for the self-update poll's direct device check -- separate
# from `_HEALTH` above, which is __init__.py's own bound name used by the
# entry's health coordinator. Patching one does not affect the other.
_POLL_HEALTH = "custom_components.kiosk_satellite_manager.ks_update.fetch_health"


def _refuses_adb():
    """An AdbClient patch that fails loudly on construction -- proves the
    self-update path never even tries to build one."""
    return patch(_ADB_CLIENT, side_effect=AssertionError("AdbClient must not be constructed"))


def _commands(**by_command):
    """run_command stand-in keyed by command name; missing keys are errors."""

    async def fake_run_command(session, host, token, command, *, pin=None):
        if command not in by_command:
            raise AssertionError(f"unexpected command: {command}")
        result = by_command[command]
        if isinstance(result, Exception):
            raise result
        if command == "getUpdateStatus":
            return {"ok": True, "data": result}
        return result

    return fake_run_command


def _release(version: str) -> ReleaseInfo:
    return ReleaseInfo(version, f"https://example.invalid/releases/{version}", f"notes for {version}")


def _health(*versions):
    """fetch_health stand-in: returns each version in turn, then repeats the last."""
    queue = list(versions)

    async def fake_fetch_health(session, host, *, pin=None):
        version = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(version, Exception):
            raise version
        return {"appVersion": version}

    return fake_fetch_health


def _entity_id(hass, entry, suffix: str) -> str:
    ent_reg = er.async_get(hass)
    return next(
        e.entity_id
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.unique_id == f"{entry.entry_id}_{suffix}"
    )


async def test_entries_share_one_15_minute_release_check(hass, release_check):
    """[KSM-TEST-130] One release coordinator for all entries, one GitHub
    request between them, removed only when the last entry unloads.

    KSM-BEHAVE-078: the first entry's setup also auto-creates the manager
    entry, which is itself a release-coordinator consumer -- so the
    coordinator only actually goes away once that entry unloads too.
    """
    with patch(_HEALTH, new=_health("2026.9.1")):
        first = await init_integration(hass)
        second = await init_integration(hass, data={CONF_HOST: "192.168.99.98"})

        coordinator = hass.data[RELEASE_COORDINATOR_KEY]
        assert coordinator.update_interval == timedelta(minutes=15)
        assert release_check.await_count == 1

        manager = next(
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER
        )

        assert await hass.config_entries.async_unload(first.entry.entry_id)
        assert hass.data[RELEASE_COORDINATOR_KEY] is coordinator

        assert await hass.config_entries.async_unload(second.entry.entry_id)
        assert hass.data[RELEASE_COORDINATOR_KEY] is coordinator

        assert await hass.config_entries.async_unload(manager.entry_id)
        assert RELEASE_COORDINATOR_KEY not in hass.data


async def test_no_update_entity_or_version_sensor_and_legacy_rows_removed(hass, release_check):
    """[KSM-TEST-259] KSM-BEHAVE-134: setup creates no update entity or version
    sensor and removes the two legacy registry rows, keeping other entities."""
    ent_reg = er.async_get(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={
        CONF_HOST: "192.168.99.99", CONF_PASSWORD: "synthetic-test-password",
    })
    entry.add_to_hass(hass)
    for domain, suffix in (("update", "update"), ("sensor", "version"), ("sensor", "keepme")):
        ent_reg.async_get_or_create(
            domain, DOMAIN, f"{entry.entry_id}_{suffix}", config_entry=entry,
        )
    with patch(_HEALTH, new=_health("2026.9.76")):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    unique_ids = {e.unique_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)}
    assert f"{entry.entry_id}_update" not in unique_ids
    assert f"{entry.entry_id}_version" not in unique_ids
    assert f"{entry.entry_id}_keepme" in unique_ids
    assert f"{entry.entry_id}_auto_update" in unique_ids
    assert not hass.states.async_entity_ids("update")


async def test_update_install_runs_the_ks_api_self_update_sequence(hass, release_check):
    """[KSM-TEST-153/227] the install goes through
    ks_update.async_self_update_entry over the KS API; AdbClient is never
    constructed, and the post-install refresh reports the new version."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76", "2026.9.77")):
        ctx = await init_integration(hass)

        with _refuses_adb() as mock_client_cls, patch(
            _POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.77"})
        ), patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(
            _RUN_COMMAND,
            new=_commands(
                getUpdateStatus={},
                installUploadedApk={"ok": True},
            ),
        ):
            await async_self_update_entry(hass, ctx.entry)

    mock_client_cls.assert_not_called()
    coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
    assert coordinator.data["appVersion"] == "2026.9.77"
    assert coordinator.ksm_installing is False


def _recording(**by_command):
    """_commands, plus the ordered list of command names sent."""
    sent: list[str] = []
    inner = _commands(**by_command)

    async def fake_run_command(session, host, token, command, *, pin=None):
        sent.append(command)
        return await inner(session, host, token, command, pin=pin)

    return fake_run_command, sent


async def test_update_install_uploads_the_cached_apk(hass, release_check, apk_upload):
    """[KSM-TEST-209] [KSM-TEST-224] KSM-BEHAVE-082/107/108 (#74): the device's
    getDeviceInfo ABIs pick the cached APK for the release, which is uploaded,
    then installUploadedApk -- never the device's own GitHub download, never
    ADB. The cache is pruned afterwards."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76", "2026.9.77")):
        ctx = await init_integration(hass)
        run, sent = _recording(
            getDeviceInfo={"ok": True, "data": {"abis": ["armeabi-v7a", "armeabi"]}},
            getUpdateStatus={},
            installUploadedApk={"ok": True},
        )
        with _refuses_adb() as adb, patch(
            _POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.77"})
        ), patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(_RUN_COMMAND, new=run):
            await async_self_update_entry(hass, ctx.entry)

    adb.assert_not_called()
    release, abis = apk_upload.release_apk.await_args.args[1:]
    assert (release.version, abis) == ("2026.9.77", ["armeabi-v7a", "armeabi"])
    [upload] = apk_upload.received
    assert upload["token"] == "device-token"
    assert upload["body"] == apk_upload.path.read_bytes()
    assert upload["size"] == apk_upload.path.stat().st_size
    # KSM-BEHAVE-186: the update status is read first, before anything uploads.
    assert sent[:3] == ["getUpdateStatus", "getDeviceInfo", "installUploadedApk"]
    assert "checkUpdateNow" not in sent and "installUpdate" not in sent
    apk_upload.prune.assert_awaited()
    assert hass.data[DOMAIN][ctx.entry.entry_id].data["appVersion"] == "2026.9.77"


async def test_update_install_refused_upload_is_failed(hass, release_check, apk_upload):
    """[KSM-TEST-209] negative case: the device refuses the upload -> failed
    with its own text; installUploadedApk is never sent."""
    release_check.return_value = _release("2026.9.77")
    apk_upload.reply = {"ok": False, "error": "Not enough free space"}
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        run, sent = _recording()
        with _refuses_adb(), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(_RUN_COMMAND, new=run):
            with pytest.raises(HomeAssistantError, match="Not enough free space"):
                await async_self_update_entry(hass, ctx.entry)

    assert "installUploadedApk" not in sent
    assert hass.data[DOMAIN][ctx.entry.entry_id].ksm_installing is False


async def test_update_install_already_running_build_installs_nothing(hass, release_check, apk_upload):
    """[KSM-TEST-209] negative case: the upload shows the build already
    running -> updated, and installUploadedApk is never sent."""
    release_check.return_value = _release("2026.9.77")
    apk_upload.reply = {"ok": True, "data": {"buildNumber": 5, "currentBuild": 5}}
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        run, sent = _recording()
        with _refuses_adb(), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(_RUN_COMMAND, new=run):
            await async_self_update_entry(hass, ctx.entry)

    # KSM-BEHAVE-186 pre-read, then the ABI read; the refresh after an updated
    # outcome may add the KSM-BEHAVE-185 status read, never an install.
    assert sent[:2] == ["getUpdateStatus", "getDeviceInfo"]
    assert set(sent[2:]) <= {"getUpdateStatus"}
    assert len(apk_upload.received) == 1


async def test_update_install_awaiting_confirmation_is_an_outcome_not_an_error(hass, release_check):
    """[KSM-TEST-155] lastOutcome: "confirm" with appVersion unchanged at
    the end of the poll window is awaiting confirmation, not an error."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)

        with _refuses_adb() as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.ks_update.SELF_UPDATE_POLL_ATTEMPTS", 1
        ), patch(
            _POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.76"})
        ), patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(
            _RUN_COMMAND,
            new=_commands(
                getUpdateStatus={"lastOutcome": "confirm"},
                installUploadedApk={"ok": True},
            ),
        ):
            outcome = await async_self_update_entry(hass, ctx.entry)

    mock_client_cls.assert_not_called()
    assert outcome == OUTCOME_AWAITING_CONFIRMATION
    assert hass.data[DOMAIN][ctx.entry.entry_id].ksm_installing is False


async def test_update_install_silent_outcome_is_failed_not_awaiting(hass, release_check):
    """[KSM-TEST-155] negative case: lastOutcome: "silent" with appVersion
    unchanged is failed, not awaiting confirmation."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)

        with _refuses_adb() as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.ks_update.SELF_UPDATE_POLL_ATTEMPTS", 1
        ), patch(
            _POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.76"})
        ), patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(
            _RUN_COMMAND,
            new=_commands(
                getUpdateStatus={"lastOutcome": "silent"},
                installUploadedApk={"ok": True},
            ),
        ):
            with pytest.raises(HomeAssistantError, match="did not complete"):
                await async_self_update_entry(hass, ctx.entry)

    mock_client_cls.assert_not_called()
    assert hass.data[DOMAIN][ctx.entry.entry_id].ksm_installing is False


async def test_update_install_reject_response_is_failed(hass, release_check):
    """[KSM-TEST-156] installUploadedApk {ok:false} yields failed with the
    device's own error text; no AdbClient."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)

        with _refuses_adb() as mock_client_cls, patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(
            _RUN_COMMAND,
            new=_commands(
                installUploadedApk={"ok": False, "error": "no space"},
            ),
        ):
            with pytest.raises(HomeAssistantError, match="no space"):
                await async_self_update_entry(hass, ctx.entry)

    mock_client_cls.assert_not_called()


async def test_update_install_poll_last_error_is_failed(hass, release_check):
    """[KSM-TEST-156/227] A device lastError raises; an ordinary error never opens ADB."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)

        fake_run_command = _commands(
            getUpdateStatus={"lastError": "boom"},
            installUploadedApk={"ok": True},
        )

        with _refuses_adb() as mock_client_cls, patch(
            _POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.76"})
        ), patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(_RUN_COMMAND, new=fake_run_command):
            with pytest.raises(HomeAssistantError, match="boom"):
                await async_self_update_entry(hass, ctx.entry)

    mock_client_cls.assert_not_called()


async def test_update_install_login_failure_is_failed(hass, release_check):
    """[KSM-TEST-156] a 401 on login yields failed; no AdbClient."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)

        with _refuses_adb() as mock_client_cls, patch(
            _LOGIN, new=AsyncMock(side_effect=KsApiError("401 Unauthorized"))
        ):
            with pytest.raises(HomeAssistantError, match="401"):
                await async_self_update_entry(hass, ctx.entry)

    mock_client_cls.assert_not_called()


async def test_update_install_no_stored_password_is_failed(hass, release_check):
    """[KSM-TEST-156] an entry with no stored password is failed before any
    KS API call or AdbClient."""
    from custom_components.kiosk_satellite_manager.const import CONF_PASSWORD

    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass, data={CONF_PASSWORD: None})

        with _refuses_adb() as mock_client_cls:
            with pytest.raises(HomeAssistantError, match="no Kiosk Satellite password"):
                await async_self_update_entry(hass, ctx.entry)

    mock_client_cls.assert_not_called()


async def test_install_refuses_while_another_install_runs(hass):
    """[KSM-TEST-133] a second install on the same entry is refused before
    any KS API call or ADB connection is opened."""
    from custom_components.kiosk_satellite_manager.ks_update import async_self_update_entry

    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
    coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
    coordinator.ksm_installing = True

    with _refuses_adb() as mock_client_cls:
        with pytest.raises(HomeAssistantError, match="already"):
            await async_self_update_entry(hass, ctx.entry)

    mock_client_cls.assert_not_called()
    assert coordinator.ksm_installing is True


async def test_auto_update_switch_defaults_off_and_persists_without_reload(hass):
    """[KSM-TEST-134] off by default; toggling only rewrites the entry option."""
    with patch(_HEALTH, new=_health("2026.9.1")):
        ctx = await init_integration(hass)
        coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
        entity_id = _entity_id(hass, ctx.entry, "auto_update")
        assert hass.states.get(entity_id).state == "off"

        await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == "on"
        assert ctx.entry.options[CONF_AUTO_UPDATE] is True

        await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
        await hass.async_block_till_done()
        assert ctx.entry.options[CONF_AUTO_UPDATE] is False

    assert ctx.entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][ctx.entry.entry_id] is coordinator


async def _publish(hass, release_check, version: str) -> None:
    release_check.return_value = _release(version)
    await hass.data[RELEASE_COORDINATOR_KEY].async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_auto_update_installs_a_newly_seen_release_once(hass, release_check):
    """[KSM-TEST-135] [KSM-TEST-260] opted in + newer release -> exactly one install."""
    release_check.return_value = _release("2026.9.76")
    with patch(_HEALTH, new=_health("2026.9.76")), patch(
        _AUTO_INSTALL, new=AsyncMock()
    ) as install:
        ctx = await init_integration(hass, options={CONF_AUTO_UPDATE: True})
        await hass.async_block_till_done(wait_background_tasks=True)
        install.assert_not_awaited()

        await _publish(hass, release_check, "2026.9.77")

    install.assert_awaited_once()
    assert install.await_args.args[1] is ctx.entry


async def test_credential_edit_does_not_trigger_auto_update(hass, release_check):
    """[KSM-TEST-166] Updating entry data does not initiate an app install."""
    release_check.return_value = _release("2026.9.76")
    with patch(_HEALTH, new=_health("2026.9.76")), patch(
        _AUTO_INSTALL, new=AsyncMock()
    ) as install:
        ctx = await init_integration(hass, options={CONF_AUTO_UPDATE: True})
        await hass.async_block_till_done(wait_background_tasks=True)
        hass.data[RELEASE_COORDINATOR_KEY].data = _release("2026.9.77")
        hass.config_entries.async_update_entry(
            ctx.entry, data={**ctx.entry.data, CONF_PASSWORD: "verified-secret"}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    install.assert_not_awaited()


async def test_auto_update_does_nothing_when_switch_off(hass, release_check):
    """[KSM-TEST-135] negative case: not opted in."""
    release_check.return_value = _release("2026.9.76")
    with patch(_HEALTH, new=_health("2026.9.76")), patch(
        _AUTO_INSTALL, new=AsyncMock()
    ) as install:
        await init_integration(hass)
        await _publish(hass, release_check, "2026.9.77")

    install.assert_not_awaited()


async def test_auto_update_never_installs_a_current_or_newer_device(hass, release_check):
    """[KSM-TEST-260] negative case: no update entity decides this any more --
    a device at or above the target is never installed."""
    release_check.return_value = _release("2026.9.77")
    for installed in ("2026.9.77", "2026.9.90"):
        with patch(_HEALTH, new=_health(installed)), patch(
            _AUTO_INSTALL, new=AsyncMock()
        ) as install:
            await init_integration(hass, options={CONF_AUTO_UPDATE: True})
            await _publish(hass, release_check, "2026.9.77")
        install.assert_not_awaited()


async def test_auto_update_skips_an_unreachable_device(hass, release_check):
    """[KSM-TEST-135] negative case: health refresh failed."""
    release_check.return_value = _release("2026.9.76")
    with patch(_HEALTH, new=_health(aiohttp.ClientError("unreachable"))), patch(
        _AUTO_INSTALL, new=AsyncMock()
    ) as install:
        await init_integration(hass, options={CONF_AUTO_UPDATE: True})
        await _publish(hass, release_check, "2026.9.77")

    install.assert_not_awaited()


async def test_auto_update_tries_each_version_once(hass, release_check):
    """[KSM-TEST-136] a failed automatic install of X is not retried on the
    next check; a newer Y is attempted."""
    release_check.return_value = _release("2026.9.76")
    with patch(_HEALTH, new=_health("2026.9.76")), patch(
        _AUTO_INSTALL, new=AsyncMock(side_effect=HomeAssistantError("boom"))
    ) as install:
        await init_integration(hass, options={CONF_AUTO_UPDATE: True})

        await _publish(hass, release_check, "2026.9.77")
        assert install.await_count == 1

        await _publish(hass, release_check, "2026.9.77")
        assert install.await_count == 1

        await _publish(hass, release_check, "2026.9.78")
        assert install.await_count == 2


async def test_auto_update_awaiting_confirmation_counts_as_the_one_attempt(hass, release_check):
    """[KSM-TEST-157] an auto-update attempt at X that ends in awaiting
    confirmation (not an exception) still counts as X's one attempt -- no
    second on-screen prompt for it -- while a newer Y is still attempted."""
    from custom_components.kiosk_satellite_manager.ks_update import OUTCOME_AWAITING_CONFIRMATION

    release_check.return_value = _release("2026.9.76")
    with patch(_HEALTH, new=_health("2026.9.76")), patch(
        _AUTO_INSTALL, new=AsyncMock(return_value=OUTCOME_AWAITING_CONFIRMATION)
    ) as install:
        await init_integration(hass, options={CONF_AUTO_UPDATE: True})

        await _publish(hass, release_check, "2026.9.77")
        assert install.await_count == 1

        await _publish(hass, release_check, "2026.9.77")
        assert install.await_count == 1

        await _publish(hass, release_check, "2026.9.78")
        assert install.await_count == 2


async def test_KSM_TEST_358_auto_update_awaiting_confirmation_is_reported(hass, release_check):
    """[KSM-TEST-358] an automatic update ending in awaiting confirmation
    creates the per-device confirmation notice instead of passing silently."""
    from custom_components.kiosk_satellite_manager.ks_update import OUTCOME_AWAITING_CONFIRMATION

    release_check.return_value = _release("2026.9.76")
    with patch(_HEALTH, new=_health("2026.9.76")), patch(
        _AUTO_INSTALL, new=AsyncMock(return_value=OUTCOME_AWAITING_CONFIRMATION)
    ), patch("custom_components.kiosk_satellite_manager.ks_update.persistent_notification") as notify:
        ctx = await init_integration(hass, options={CONF_AUTO_UPDATE: True})
        await _publish(hass, release_check, "2026.9.77")
        await hass.async_block_till_done(wait_background_tasks=True)

    notify.async_create.assert_called_once()
    assert notify.async_create.call_args.kwargs["notification_id"].endswith(ctx.entry.entry_id)


def test_is_older_ignores_unknown_and_unparseable_versions():
    """[KSM-TEST-358] An unknown or unparseable version never triggers an auto-update."""
    from custom_components.kiosk_satellite_manager.auto_update import is_older

    assert is_older("2026.10.4", "2026.10.5") is True
    assert is_older("2026.10.5", "2026.10.5") is False
    assert is_older(None, "2026.10.5") is False
    assert is_older("2026.10.4", "") is False
    assert is_older("not a version", "2026.10.5") is False


async def test_KSM_TEST_372_health_refresh_reports_a_device_side_failure(hass, release_check):
    """[KSM-TEST-372] in a real hass, a loaded device's health refresh reads
    getUpdateStatus and raises the per-device failure notice; a clean read
    after the next refresh dismisses it."""
    prefix = "custom_components.kiosk_satellite_manager.update_failure."
    statuses = [{"lastOutcome": "failed", "lastError": "disk full"}]

    async def run_command(session, host, token, command, *, pin=None):
        assert command == "getUpdateStatus"
        return {"ok": True, "data": statuses[-1]}

    with patch(_HEALTH, new=_health("2026.9.1")), patch(
        prefix + "ks_api_client.login", new=AsyncMock(return_value="device-token")
    ), patch(prefix + "ks_api_client.run_command", new=run_command), patch(
        prefix + "persistent_notification"
    ) as notify:
        ctx = await init_integration(hass)
        coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        notice = f"{DOMAIN}_update_failed_{ctx.entry.entry_id}"
        created = [c.kwargs for c in notify.async_create.call_args_list]
        assert [c["notification_id"] for c in created] == [notice]
        assert "disk full" in created[0]["message"]

        statuses.append({"lastOutcome": "silent", "lastError": None})
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        notify.async_dismiss.assert_called_with(hass, notice)
        assert notify.async_create.call_count == 1
