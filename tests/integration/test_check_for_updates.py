"""Manager Check for updates button and new-release device fan-out
(KSM-BEHAVE-103, kiosk-satellite-manager#66)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.kiosk_satellite_manager.const import RELEASE_COORDINATOR_KEY
from custom_components.kiosk_satellite_manager.ks_api import ReleaseInfo

from .conftest import init_integration
from .test_global_settings import _entity, _manager, _publish

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"


async def _press(hass, entity_id):
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_check_for_updates_button_runs_the_release_check_now(hass, release_check):
    """[KSM-TEST-195] Manager-only button; each press checks GitHub at once."""
    manager = await _manager(hass)
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.1"})):
        device = await init_integration(hass)
    device_ids = {
        e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), device.entry.entry_id)
    }
    assert f"{device.entry.entry_id}_check_for_updates" not in device_ids
    button = _entity(hass, manager, "check_for_updates")
    sensor = _entity(hass, manager, "latest_release")

    before = release_check.await_count
    release_check.return_value = ReleaseInfo("2026.9.2", "https://example.invalid/2", "notes")
    await _press(hass, button)
    assert release_check.await_count == before + 1
    assert hass.states.get(sensor).state == "2026.9.2"

    # A second press straight after is not swallowed by a refresh debounce.
    release_check.side_effect = RuntimeError("github unreachable")
    with pytest.raises(HomeAssistantError, match="github unreachable"):
        await _press(hass, button)
    assert release_check.await_count == before + 2
    assert hass.states.get(sensor).state == "2026.9.2"


async def test_only_a_newly_seen_release_fans_out_to_devices(
    hass, release_check, device_update_check
):
    """[KSM-TEST-196] Baseline first; one fan-out per new version, from any trigger."""
    manager = await _manager(hass)
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.1"})):
        await init_integration(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        device_update_check.assert_not_awaited()

        await _publish(hass, release_check, "2026.9.1")
        device_update_check.assert_not_awaited()

        release_check.side_effect = RuntimeError("offline")
        await hass.data[RELEASE_COORDINATOR_KEY].async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        device_update_check.assert_not_awaited()
        release_check.side_effect = None

        await _publish(hass, release_check, "2026.9.2")
        device_update_check.assert_awaited_once_with(hass)
        await _publish(hass, release_check, "2026.9.2")
        assert device_update_check.await_count == 1

        release_check.return_value = ReleaseInfo("2026.9.3", "https://example.invalid/3", "notes")
        await _press(hass, _entity(hass, manager, "check_for_updates"))
        assert device_update_check.await_count == 2


async def test_device_setup_checks_its_release_even_at_startup_baseline(
    hass, release_check, device_update_check
):
    """[KSM-TEST-226] Setup refreshes the ESPHome release view without a version change."""
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.1"})), patch(
        "custom_components.kiosk_satellite_manager.async_check_device_for_update",
        new=AsyncMock(side_effect=RuntimeError("device check failed")), create=True,
    ) as check:
        first = await init_integration(hass)
        assert hass.config_entries.async_get_entry(first.entry.entry_id) is not None
        check.assert_awaited_once()
        assert check.await_args.args[1] is first.entry
        assert release_check.await_count >= 1
        device_update_check.assert_not_awaited()

    assert hass.data[RELEASE_COORDINATOR_KEY].update_interval.total_seconds() == 15 * 60
