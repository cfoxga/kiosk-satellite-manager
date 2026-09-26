"""Kiosk Satellite update entity, shared release check, and opt-in
auto-update (KSM-BEHAVE-071/072/073/082, issues #43, #47). The GitHub
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

from .conftest import init_integration

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_AUTO_INSTALL = "custom_components.kiosk_satellite_manager.update.async_self_update_entry"
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


async def test_entries_share_one_hourly_release_check(hass, release_check):
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
        assert coordinator.update_interval == timedelta(hours=1)
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


async def test_update_entity_reports_installed_and_latest(hass, release_check, hass_ws_client):
    """[KSM-TEST-131] installed from /api/health; latest, URL and notes
    from the release check; an older install reads `on`."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        state = hass.states.get(entity_id)
        assert state.state == "on"
        assert state.attributes["installed_version"] == "2026.9.76"
        assert state.attributes["latest_version"] == "2026.9.77"
        assert state.attributes["release_url"] == "https://example.invalid/releases/2026.9.77"
        assert state.attributes["title"] == "Kiosk Satellite"

        client = await hass_ws_client(hass)
        await client.send_json({"id": 1, "type": "update/release_notes", "entity_id": entity_id})
        result = await client.receive_json()
        assert result["success"] is True
        assert result["result"] == "notes for 2026.9.77"


async def test_update_entity_is_off_when_current(hass, release_check):
    """[KSM-TEST-131] negative case: installed == latest is not an update."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.77")):
        ctx = await init_integration(hass)
        assert hass.states.get(_entity_id(hass, ctx.entry, "update")).state == "off"


async def test_update_install_runs_the_ks_api_self_update_sequence(hass, release_check):
    """[KSM-TEST-153] update.install goes through
    ks_update.async_self_update_entry over the KS API; AdbClient is never
    constructed, and the post-install refresh reports the new version."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76", "2026.9.77")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        with _refuses_adb() as mock_client_cls, patch(
            _POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.77"})
        ), patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(
            _RUN_COMMAND,
            new=_commands(
                checkUpdateNow={},
                getUpdateStatus={"availableVersion": "2026.9.77"},
                getUpdateInstallerStatus={},
                installUpdate={"ok": True},
            ),
        ):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )

    mock_client_cls.assert_not_called()
    state = hass.states.get(entity_id)
    assert state.attributes["installed_version"] == "2026.9.77"
    assert state.state == "off"
    assert state.attributes["in_progress"] is False


async def test_update_install_not_offered_raises_naming_the_version(hass, release_check):
    """[KSM-TEST-154] getUpdateStatus.availableVersion != V after
    checkUpdateNow -> installUpdate is never called, HomeAssistantError
    names V."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        with _refuses_adb() as mock_client_cls, patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(
            _RUN_COMMAND,
            new=_commands(checkUpdateNow={}, getUpdateStatus={"availableVersion": "2026.9.76"}),
        ):
            with pytest.raises(HomeAssistantError, match="2026.9.77"):
                await hass.services.async_call(
                    "update", "install", {"entity_id": entity_id}, blocking=True
                )

    mock_client_cls.assert_not_called()
    assert hass.data[DOMAIN][ctx.entry.entry_id].ksm_installing is False


@pytest.mark.parametrize("status", [
    {"ok": False, "data": {"availableVersion": "2026.9.77"}},
    {"ok": True},
])
async def test_update_install_rejects_bad_status_envelope(hass, release_check, status):
    """[KSM-TEST-154] A failed or missing status body cannot authorize install."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        async def command(session, host, token, name, *, pin=None):
            if name == "checkUpdateNow":
                return {"ok": True}
            if name == "getUpdateStatus":
                return status
            raise AssertionError(f"unexpected command: {name}")

        with _refuses_adb() as adb, patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(_RUN_COMMAND, new=command):
            with pytest.raises(HomeAssistantError, match="status"):
                await hass.services.async_call(
                    "update", "install", {"entity_id": entity_id}, blocking=True
                )
    adb.assert_not_called()
    assert hass.data[DOMAIN][ctx.entry.entry_id].ksm_installing is False


async def test_update_install_awaiting_confirmation_sets_the_attribute(hass, release_check):
    """[KSM-TEST-155] lastOutcome: "confirm" with appVersion unchanged at
    the end of the poll window is awaiting confirmation, not an error; the
    attribute clears once health later reports V."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        with _refuses_adb() as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.ks_update.SELF_UPDATE_POLL_ATTEMPTS", 1
        ), patch(
            _POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.76"})
        ), patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(
            _RUN_COMMAND,
            new=_commands(
                checkUpdateNow={},
                getUpdateStatus={"availableVersion": "2026.9.77", "lastOutcome": "confirm"},
                getUpdateInstallerStatus={},
                installUpdate={"ok": True},
            ),
        ):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )

    mock_client_cls.assert_not_called()
    state = hass.states.get(entity_id)
    assert state.attributes["ksm_update_state"] == "awaiting_confirmation"
    assert state.attributes["in_progress"] is False

    with patch(_HEALTH, new=_health("2026.9.77")):
        await hass.data[DOMAIN][ctx.entry.entry_id].async_refresh()
    assert hass.states.get(entity_id).attributes.get("ksm_update_state") is None


async def test_update_install_silent_outcome_is_failed_not_awaiting(hass, release_check):
    """[KSM-TEST-155] negative case: lastOutcome: "silent" with appVersion
    unchanged is failed, not awaiting confirmation."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        with _refuses_adb() as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.ks_update.SELF_UPDATE_POLL_ATTEMPTS", 1
        ), patch(
            _POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.76"})
        ), patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(
            _RUN_COMMAND,
            new=_commands(
                checkUpdateNow={},
                getUpdateStatus={"availableVersion": "2026.9.77", "lastOutcome": "silent"},
                getUpdateInstallerStatus={},
                installUpdate={"ok": True},
            ),
        ):
            with pytest.raises(HomeAssistantError, match="did not complete"):
                await hass.services.async_call(
                    "update", "install", {"entity_id": entity_id}, blocking=True
                )

    mock_client_cls.assert_not_called()
    assert hass.data[DOMAIN][ctx.entry.entry_id].ksm_installing is False


async def test_update_install_reject_response_is_failed(hass, release_check):
    """[KSM-TEST-156] installUpdate {ok:false} yields failed with the
    device's own error text; no AdbClient."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        with _refuses_adb() as mock_client_cls, patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(
            _RUN_COMMAND,
            new=_commands(
                checkUpdateNow={},
                getUpdateStatus={"availableVersion": "2026.9.77"},
                getUpdateInstallerStatus={},
                installUpdate={"ok": False, "error": "no space"},
            ),
        ):
            with pytest.raises(HomeAssistantError, match="no space"):
                await hass.services.async_call(
                    "update", "install", {"entity_id": entity_id}, blocking=True
                )

    mock_client_cls.assert_not_called()


async def test_update_install_poll_last_error_is_failed(hass, release_check):
    """[KSM-TEST-156] a non-null lastError surfacing from the poll's own
    getUpdateStatus (after a successful installUpdate) yields failed; no
    AdbClient."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        status_calls = {"n": 0}

        async def fake_run_command(session, host, token, command, *, pin=None):
            if command == "checkUpdateNow":
                return {}
            if command == "getUpdateStatus":
                status_calls["n"] += 1
                if status_calls["n"] == 1:
                    return {"ok": True, "data": {"availableVersion": "2026.9.77"}}
                return {"ok": True, "data": {"availableVersion": "2026.9.77", "lastError": "boom"}}
            if command == "getUpdateInstallerStatus":
                return {}
            if command == "installUpdate":
                return {"ok": True}
            raise AssertionError(f"unexpected command: {command}")

        with _refuses_adb() as mock_client_cls, patch(
            _POLL_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.76"})
        ), patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(_RUN_COMMAND, new=fake_run_command):
            with pytest.raises(HomeAssistantError, match="boom"):
                await hass.services.async_call(
                    "update", "install", {"entity_id": entity_id}, blocking=True
                )

    mock_client_cls.assert_not_called()


async def test_update_install_login_failure_is_failed(hass, release_check):
    """[KSM-TEST-156] a 401 on login yields failed; no AdbClient."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        with _refuses_adb() as mock_client_cls, patch(
            _LOGIN, new=AsyncMock(side_effect=KsApiError("401 Unauthorized"))
        ):
            with pytest.raises(HomeAssistantError, match="401"):
                await hass.services.async_call(
                    "update", "install", {"entity_id": entity_id}, blocking=True
                )

    mock_client_cls.assert_not_called()


async def test_update_install_no_stored_password_is_failed(hass, release_check):
    """[KSM-TEST-156] an entry with no stored password is failed before any
    KS API call or AdbClient."""
    from custom_components.kiosk_satellite_manager.const import CONF_PASSWORD

    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass, data={CONF_PASSWORD: None})
        entity_id = _entity_id(hass, ctx.entry, "update")

        with _refuses_adb() as mock_client_cls:
            with pytest.raises(HomeAssistantError, match="no Kiosk Satellite password"):
                await hass.services.async_call(
                    "update", "install", {"entity_id": entity_id}, blocking=True
                )

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
    """[KSM-TEST-135] opted in + newer release -> exactly one install."""
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


async def test_auto_update_respects_a_skipped_version(hass, release_check):
    """[KSM-TEST-135] negative case: HA's own skip wins over auto-update."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")), patch(
        _AUTO_INSTALL, new=AsyncMock()
    ) as install:
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")
        await hass.services.async_call("update", "skip", {"entity_id": entity_id}, blocking=True)
        assert hass.states.get(entity_id).state == "off"

        switch_id = _entity_id(hass, ctx.entry, "auto_update")
        await hass.services.async_call("switch", "turn_on", {"entity_id": switch_id}, blocking=True)
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
