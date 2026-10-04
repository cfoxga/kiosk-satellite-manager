"""A device-side update failure is reported (KSM-BEHAVE-185, #183).

No hass fixture: hass is a fake holding only `data`; the notification
module, the API client and the ADB port probe are patched at the boundary.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.kiosk_satellite_manager import ks_update, update_failure
from custom_components.kiosk_satellite_manager.const import (
    CONF_DEVICE_PROFILE,
    CONF_HOST,
    CONF_PASSWORD,
    DOMAIN,
)

_PREFIX = "custom_components.kiosk_satellite_manager.update_failure."
_VERIFIER = (
    "a package verifier on this device rejected the update "
    "(INSTALL_FAILED_VERIFICATION_FAILURE); on a Meta Portal turn it off over adb"
)


def _entry(**data):
    return SimpleNamespace(
        data={CONF_HOST: "192.168.40.133", CONF_PASSWORD: "secret", **data},
        title="Kitchen Portal",
        entry_id="kitchen",
    )


def _hass(*, installing=False, healthy=True):
    coordinator = SimpleNamespace(ksm_installing=installing, last_update_success=healthy)
    return SimpleNamespace(data={DOMAIN: {"kitchen": coordinator}})


async def _poll(hass, entry, status, *, adb_open=False, login=None):
    """Run one poll with getUpdateStatus answering `status`; returns the
    notification mock, the login mock and the ADB probe mock."""
    login = login or AsyncMock(return_value="device-token")
    run = AsyncMock(return_value={"ok": True, "data": status})
    with patch(_PREFIX + "async_get_clientsession"), patch(
        _PREFIX + "ks_api_client.login", new=login
    ), patch(_PREFIX + "ks_api_client.run_command", new=run), patch(
        _PREFIX + "async_probe_adb_port", new=AsyncMock(return_value=adb_open)
    ) as probe, patch(_PREFIX + "persistent_notification") as notify:
        await update_failure.async_poll(hass, entry)
    if run.await_count:
        assert run.await_args.args[3] == "getUpdateStatus"
    return notify, login, probe


async def test_KSM_TEST_369_device_failure_is_reported_and_cleared():
    """[KSM-TEST-369] a non-verifier lastError is quoted with no ADB action;
    a clean status dismisses; an installing or password-less device is not read."""
    hass, entry = _hass(), _entry()
    notify, _, probe = await _poll(hass, entry, {"lastOutcome": "failed", "lastError": "disk full"})
    notify.async_create.assert_called_once()
    kwargs = notify.async_create.call_args.kwargs
    assert kwargs["notification_id"] == f"{DOMAIN}_update_failed_kitchen"
    assert "Kitchen Portal" in kwargs["title"]
    assert "disk full" in kwargs["message"]
    assert "ADB" not in kwargs["message"]
    probe.assert_not_awaited()

    notify, _, _ = await _poll(hass, entry, {"lastOutcome": "silent", "lastError": None})
    notify.async_create.assert_not_called()
    notify.async_dismiss.assert_called_once_with(hass, f"{DOMAIN}_update_failed_kitchen")

    # lastOutcome failed alone, no error text, still counts as failed.
    notify, _, _ = await _poll(_hass(), entry, {"lastOutcome": "failed"})
    notify.async_create.assert_called_once()

    for hass, entry in ((_hass(installing=True), _entry()),
                        (_hass(healthy=False), _entry()),
                        (_hass(), _entry(**{CONF_PASSWORD: None}))):
        notify, login, _ = await _poll(hass, entry, {"lastError": "disk full"})
        login.assert_not_awaited()
        notify.async_create.assert_not_called()
        notify.async_dismiss.assert_not_called()


@pytest.mark.parametrize("adb_open", [False, True])
async def test_KSM_TEST_370_verifier_rejection_gives_the_action(adb_open):
    """[KSM-TEST-370] a Portal-recipe verifier rejection says what to do,
    depending on whether ADB answers; never "tap the prompt"."""
    entry = _entry(**{CONF_DEVICE_PROFILE: "portal_plus_gen2"})
    notify, _, probe = await _poll(
        _hass(), entry, {"lastOutcome": "failed", "lastError": _VERIFIER}, adb_open=adb_open
    )
    probe.assert_awaited_once_with("192.168.40.133", 5555)
    message = notify.async_create.call_args.kwargs["message"]
    assert "INSTALL_FAILED_VERIFICATION_FAILURE" in message
    assert "Install Kiosk Satellite" in message
    assert "tap" not in message.lower()
    assert ("Turn ADB on" in message) is (not adb_open)


async def test_KSM_TEST_370_no_recipe_gets_no_adb_action():
    """[KSM-TEST-370] negative case: the same rejection on a device with no
    approved recipe is quoted, with no ADB action and no probe."""
    notify, _, probe = await _poll(_hass(), _entry(), {"lastError": _VERIFIER})
    probe.assert_not_awaited()
    message = notify.async_create.call_args.kwargs["message"]
    assert "INSTALL_FAILED_VERIFICATION_FAILURE" in message
    assert "ADB" not in message


async def test_KSM_TEST_371_unchanged_failure_is_not_resent():
    """[KSM-TEST-371] the same text is posted once; a changed text again; a
    failed read creates and dismisses nothing."""
    hass, entry = _hass(), _entry()
    first, _, _ = await _poll(hass, entry, {"lastError": "disk full"})
    again, _, _ = await _poll(hass, entry, {"lastError": "disk full"})
    changed, _, _ = await _poll(hass, entry, {"lastError": "signature mismatch"})
    first.async_create.assert_called_once()
    again.async_create.assert_not_called()
    changed.async_create.assert_called_once()

    notify, _, _ = await _poll(hass, entry, {}, login=AsyncMock(side_effect=OSError("refused")))
    notify.async_create.assert_not_called()
    notify.async_dismiss.assert_not_called()

    # A refused or malformed status envelope is a failed read too.
    run = AsyncMock(return_value={"ok": False, "error": "unauthorized"})
    with patch(_PREFIX + "async_get_clientsession"), patch(
        _PREFIX + "ks_api_client.login", new=AsyncMock(return_value="t")
    ), patch(_PREFIX + "ks_api_client.run_command", new=run), patch(
        _PREFIX + "persistent_notification"
    ) as notify:
        await update_failure.async_poll(hass, entry)
    notify.async_create.assert_not_called()
    notify.async_dismiss.assert_not_called()


async def test_KSM_TEST_372_updated_outcome_dismisses_the_failure():
    """[KSM-TEST-372] a KSM update ending updated dismisses the failure
    notice, and a later identical failure is posted again."""
    hass, entry = _hass(), _entry()
    await _poll(hass, entry, {"lastError": "disk full"})
    with patch("custom_components.kiosk_satellite_manager.ks_update.persistent_notification"), \
            patch(_PREFIX + "persistent_notification") as notify:
        ks_update.notify_awaiting_confirmation(hass, entry, ks_update.OUTCOME_UPDATED)
    notify.async_dismiss.assert_called_once_with(hass, f"{DOMAIN}_update_failed_kitchen")

    notify, _, _ = await _poll(hass, entry, {"lastError": "disk full"})
    notify.async_create.assert_called_once()

    with patch("custom_components.kiosk_satellite_manager.ks_update.persistent_notification"), \
            patch(_PREFIX + "persistent_notification") as notify:
        ks_update.notify_awaiting_confirmation(hass, entry, ks_update.OUTCOME_AWAITING_CONFIRMATION)
    notify.async_dismiss.assert_not_called()
