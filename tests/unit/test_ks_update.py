"""Unit tests for the self-update sequence (KSM-BEHAVE-082, #47) that exercise
branches the integration suite's happy-path/ATTEMPTS=1 shape doesn't reach:
no coordinator registered for the entry, no usable release, a transient
failure mid-install, and the multi-attempt poll loop (including its
between-attempts sleep). No real hass fixture -- hass/entry are lightweight
fakes, since async_self_update_entry only reads hass.data and entry.data/title.
"""
from __future__ import annotations

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
    return SimpleNamespace(data=data)


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
            checkUpdateNow={},
            getUpdateStatus={"availableVersion": "2026.9.77"},
            getUpdateInstallerStatus={},
            installUpdate={"ok": True},
        ),
    ):
        outcome = await ks_update.async_self_update_entry(hass, _entry())
    assert outcome == ks_update.OUTCOME_UPDATED


async def test_install_update_transient_failure_is_reported_as_failed():
    """[KSM-TEST-156] A transient error on installUpdate itself (not just
    login/checkUpdateNow) fails loudly, naming the entry."""
    hass = _hass()

    async def fake_run_command(session, host, token, command, *, pin=None):
        if command == "installUpdate":
            raise KsApiError("connection reset")
        return {
            "checkUpdateNow": {},
            "getUpdateStatus": {"ok": True, "data": {"availableVersion": "2026.9.77"}},
            "getUpdateInstallerStatus": {},
        }[command]

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
