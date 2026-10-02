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


async def test_device_setup_checks_its_release_even_after_first_detection(
    hass, release_check, device_update_check
):
    """[KSM-TEST-226] Setup refreshes the ESPHome view even after detection."""
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.1"})), patch(
        "custom_components.kiosk_satellite_manager.async_check_device_for_update",
        new=AsyncMock(side_effect=RuntimeError("device check failed")), create=True,
    ) as check:
        first = await init_integration(hass)
        assert hass.config_entries.async_get_entry(first.entry.entry_id) is not None
        check.assert_awaited_once()
        assert check.await_args.args[1] is first.entry
        assert release_check.await_count >= 1
        # Auto-created manager may have detected the release after this device
        # was registered; the per-device setup check still runs independently.

    assert hass.data[RELEASE_COORDINATOR_KEY].update_interval.total_seconds() == 15 * 60


async def test_first_release_detection_refreshes_loaded_devices(
    hass, release_check, device_update_check
):
    """[KSM-TEST-228] Detection itself refreshes devices present when KSM starts."""
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.1"})):
        await init_integration(hass)
        coordinator = hass.data[RELEASE_COORDINATOR_KEY]
        coordinator.ksm_announced_version = None
        device_update_check.reset_mock()
        device_update_check.assert_not_awaited()

        release_check.side_effect = RuntimeError("github offline")
        await coordinator.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        device_update_check.assert_not_awaited()
        release_check.side_effect = None

        await _publish(hass, release_check, "2026.9.1")
        device_update_check.assert_awaited_once_with(hass)
        await _publish(hass, release_check, "2026.9.1")
        assert device_update_check.await_count == 1


_RELEASES = ReleaseInfo(
    "2026.10.3", "https://example.invalid/3", "notes 3",
    (("kiosk-satellite-2026.10.3.arm64-v8a.apk", "https://example.invalid/3.apk"),),
)
_RELEASES = ReleaseInfo(
    _RELEASES.version, _RELEASES.url, _RELEASES.notes, _RELEASES.assets,
    recent=(_RELEASES, ReleaseInfo(
        "2026.10.2", "https://example.invalid/2", "notes 2",
        (("kiosk-satellite-2026.10.2.arm64-v8a.apk", "https://example.invalid/2.apk"),),
    )),
)
_STORE_KEY = "kiosk_satellite_manager.release_check"


async def _restart_release_check(hass):
    """A new HA run: the in-memory coordinator is gone, the HA Store is not."""
    from custom_components.kiosk_satellite_manager import _async_ensure_release_coordinator

    old = hass.data.pop(RELEASE_COORDINATOR_KEY)
    await old.async_shutdown()
    await _async_ensure_release_coordinator(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    return hass.data[RELEASE_COORDINATOR_KEY]


async def test_last_successful_release_check_survives_restart(hass, hass_storage, release_check):
    """[KSM-TEST-311] KSM-BEHAVE-155 (#127): a rate-limited first check after a
    restart keeps the last successful check's releases as the install target."""
    from custom_components.kiosk_satellite_manager.helpers import recent_releases, target_release

    release_check.return_value = _RELEASES
    await _manager(hass)
    assert target_release(hass).version == "2026.10.3"
    assert _STORE_KEY in hass_storage

    release_check.side_effect = RuntimeError("403, message='rate limit exceeded'")
    coordinator = await _restart_release_check(hass)
    assert coordinator.last_update_success is False
    assert getattr(coordinator, "ksm_last_success", None) is None
    target = target_release(hass)
    assert target is not None and target.version == "2026.10.3"
    assert target.assets == _RELEASES.assets
    assert [r.version for r in recent_releases(hass)] == ["2026.10.3", "2026.10.2"]
    assert recent_releases(hass)[1].assets == _RELEASES.recent[1].assets


async def test_failed_startup_without_saved_check_has_no_target(hass, hass_storage, release_check):
    """[KSM-TEST-311] negative: nothing saved, a failed startup check leaves no target."""
    from custom_components.kiosk_satellite_manager.helpers import target_release

    release_check.side_effect = RuntimeError("403, message='rate limit exceeded'")
    await _manager(hass)
    assert target_release(hass) is None
    assert _STORE_KEY not in hass_storage


async def test_failed_check_keeps_store_and_seed_still_announces(
    hass, hass_storage, release_check, device_update_check
):
    """[KSM-TEST-312] KSM-BEHAVE-155: a failed check never rewrites the store, and
    a seeded startup's first real success still fans the version out once."""
    release_check.return_value = _RELEASES
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.10.2"})):
        await init_integration(hass)
        saved = dict(hass_storage[_STORE_KEY])

        release_check.side_effect = RuntimeError("offline")
        await hass.data[RELEASE_COORDINATOR_KEY].async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert hass_storage[_STORE_KEY] == saved

        coordinator = await _restart_release_check(hass)
        assert coordinator.data.version == "2026.10.3"
        assert coordinator.ksm_announced_version is None
        device_update_check.reset_mock()

        release_check.side_effect = None
        await coordinator.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        device_update_check.assert_awaited_once_with(hass)
        assert coordinator.ksm_last_success is not None
