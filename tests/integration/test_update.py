"""Kiosk Satellite update entity, shared release check, and opt-in
auto-update (KSM-BEHAVE-071/072/073, issue #43). The GitHub release check
and the device's /api/health are mocked at the boundary (see conftest's
`release_check`); install goes through the real entry-level install sequence
in button.py with AdbClient/install_and_launch mocked, so these tests prove
the update entity reuses the button's verified path rather than a copy.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.kiosk_satellite_manager.button import async_install_entry
from custom_components.kiosk_satellite_manager.const import (
    CONF_AUTO_UPDATE,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    DOMAIN,
    ENTRY_TYPE_MANAGER,
    RELEASE_COORDINATOR_KEY,
)
from custom_components.kiosk_satellite_manager.install import KsInstallVerificationFailed
from custom_components.kiosk_satellite_manager.ks_api import ReleaseInfo

from .conftest import init_integration

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_AUTO_INSTALL = "custom_components.kiosk_satellite_manager.update.async_install_entry"


def _release(version: str) -> ReleaseInfo:
    return ReleaseInfo(version, f"https://example.invalid/releases/{version}", f"notes for {version}")


def _health(*versions):
    """fetch_health stand-in: returns each version in turn, then repeats the last."""
    queue = list(versions)

    async def fake_fetch_health(session, host):
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


async def test_update_install_runs_the_button_install_sequence(hass, release_check):
    """[KSM-TEST-132] update.install goes through button.async_install_entry
    and the post-install refresh reports the new version."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76", "2026.9.77")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        seen_in_progress = []

        async def fake_install(*args, **kwargs):
            seen_in_progress.append(hass.states.get(entity_id).attributes["in_progress"])

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.button.install_and_launch",
            new=AsyncMock(side_effect=fake_install),
        ) as install:
            mock_client_cls.return_value.connect = AsyncMock()
            mock_client_cls.return_value.close = AsyncMock()
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )

    install.assert_awaited_once()
    assert seen_in_progress == [True]
    state = hass.states.get(entity_id)
    assert state.attributes["installed_version"] == "2026.9.77"
    assert state.state == "off"
    assert state.attributes["in_progress"] is False


async def test_update_install_failure_surfaces_as_service_error(hass, release_check):
    """[KSM-TEST-132] negative case: a failed install is an error to the
    caller, and the in-progress flag is cleared."""
    release_check.return_value = _release("2026.9.77")
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
        entity_id = _entity_id(hass, ctx.entry, "update")

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.button.install_and_launch",
            new=AsyncMock(side_effect=KsInstallVerificationFailed("versionName mismatch")),
        ):
            mock_client_cls.return_value.connect = AsyncMock()
            mock_client_cls.return_value.close = AsyncMock()
            with pytest.raises(HomeAssistantError, match="versionName mismatch"):
                await hass.services.async_call(
                    "update", "install", {"entity_id": entity_id}, blocking=True
                )

    assert hass.data[DOMAIN][ctx.entry.entry_id].ksm_installing is False
    assert hass.states.get(entity_id).attributes["in_progress"] is False


async def test_install_refuses_while_another_install_runs(hass):
    """[KSM-TEST-133] a second install on the same entry is refused before
    any ADB connection is opened."""
    with patch(_HEALTH, new=_health("2026.9.76")):
        ctx = await init_integration(hass)
    coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
    coordinator.ksm_installing = True

    with patch("custom_components.kiosk_satellite_manager.button.AdbClient") as mock_client_cls:
        with pytest.raises(HomeAssistantError, match="already"):
            await async_install_entry(hass, ctx.entry)

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
