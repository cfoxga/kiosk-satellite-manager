"""KSM-TEST-216 (kiosk-satellite-manager#54): after Device Owner, KSM turns the
kiosk lock off, shows Meta setup, watches for the Meta login, and turns the
lock back on -- against the scripted Portal shell and a fake Kiosk Satellite
settings API."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.kiosk_satellite_manager import device_owner, meta_setup
from custom_components.kiosk_satellite_manager.device_owner import DeviceOwnerError
from ksm_device_owner_fake import KS_ACTIVITY, SETUP_ACTIVITY, FakeDevice

HOST = "192.0.2.71"
FULL = tuple(f"com.facebook.aloha.{t}" for t in ("hw", "pl", "privowner", "sso"))
_NOTIFY = "custom_components.kiosk_satellite_manager.meta_setup.persistent_notification.async_create"


class FakeKs:
    def __init__(self, settings: dict, *, fail: bool = False):
        self.settings = dict(settings)
        self.patches: list[dict] = []
        self.fail = fail

    async def login(self, session, host, password, *, pin):
        if self.fail:
            raise meta_setup.KsApiError("unreachable")
        assert (host, password) == (HOST, "pw")
        return "tok"

    async def get_settings(self, session, host, token, *, pin):
        return dict(self.settings)

    async def patch_settings(self, session, host, token, values, *, pin):
        self.patches.append(dict(values))
        self.settings.update(values)
        return {}


class Portal(FakeDevice):
    """Meta setup 'finishes' (the login accounts return) after `polls`
    account reads by the watcher; `drop_once` fails one read like a
    dropped ADB transport."""

    def __init__(self, *, polls: int | None = 2, drop_once: bool = False, **kw):
        super().__init__(owner="me.jxl.kiosk_satellite",
                         account_types=("com.facebook.aloha.hw",), **kw)
        self.polls = polls
        self.drop_once = drop_once
        self.reads = 0

    async def shell(self, command: str) -> str:
        if command == "dumpsys account":
            self.reads += 1
            if self.drop_once:
                self.drop_once = False
                raise ConnectionResetError("adb transport closed")
            if self.polls is not None and self.reads >= self.polls:
                self.account_types = list(FULL)
        return await super().shell(command)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(device_owner, "_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(meta_setup, "WATCH_INTERVAL_S", 0)


def _target(password="pw"):
    return meta_setup.Target(
        host=HOST, port=5555, key_path="/k/adbkey", password=password, pin=None,
        model_key="portal_go", name="Go",
    )


def _patches(ks: FakeKs, portal: Portal):
    connects: list[int] = []

    class Client:
        def __init__(self, host, port, key_path):
            assert (host, port, key_path) == (HOST, 5555, "/k/adbkey")

        async def connect(self, auth_timeout_s: float = 5):
            connects.append(1)

        async def close(self):
            pass

        async def shell(self, command):
            return await portal.shell(command)

    return (
        patch.object(meta_setup.ks_api_client, "login", ks.login),
        patch.object(meta_setup.ks_api_client, "get_settings", ks.get_settings),
        patch.object(meta_setup.ks_api_client, "patch_settings", ks.patch_settings),
        patch.object(meta_setup, "AdbClient", Client),
        patch(_NOTIFY),
    ), connects


async def _run(hass, ks, portal, target=None):
    (a, b, c, d, n), connects = _patches(ks, portal)
    with a, b, c, d, n as notify:
        await meta_setup.async_start(hass, target or _target(), portal)
        started = [call.kwargs["message"] for call in notify.call_args_list]
        await hass.async_block_till_done(wait_background_tasks=True)
    messages = [call.kwargs["message"] for call in notify.call_args_list]
    return started, messages, connects


async def test_lock_off_setup_shown_watch_restores_lock(hass):
    """[KSM-TEST-216] Only the lock settings that were on are turned off;
    Meta setup is in front; once the login accounts return the same
    settings go back on and the notice says so."""
    ks = FakeKs({"kiosk.enabled": True, "lockdown.enabled": False})
    portal = Portal(polls=3)
    started, messages, _ = await _run(hass, ks, portal)
    assert ks.patches[0] == {"kiosk.enabled": False}
    assert portal.front == SETUP_ACTIVITY
    assert "showing Meta's setup screen" in started[-1]
    assert "Kiosk mode is off" in started[-1]
    assert ks.patches[-1] == {"kiosk.enabled": True}
    assert "lockdown.enabled" not in str(ks.patches)
    assert portal.reads >= 3
    assert "Meta setup is finished" in messages[-1]
    assert "Kiosk mode is back on" in messages[-1]
    assert "ADB debugging is off" not in messages[-1]
    assert all(m.startswith("Go: ") for m in messages)


async def test_watch_survives_a_dropped_connection(hass):
    """[KSM-TEST-216] A transport error mid-watch reconnects instead of
    ending the watch."""
    ks = FakeKs({"kiosk.enabled": True})
    portal = Portal(polls=3, drop_once=True)
    _, messages, connects = await _run(hass, ks, portal)
    assert len(connects) == 2
    assert "Meta setup is finished" in messages[-1]


async def test_watch_timeout_leaves_lock_off_and_says_so(hass, monkeypatch):
    """[KSM-TEST-216] Negative: setup never finishing ends the watch with a
    notice naming the retry path; the lock is not turned back on while the
    Meta login is still missing."""
    monkeypatch.setattr(meta_setup, "WATCH_TIMEOUT_S", 0)
    ks = FakeKs({"kiosk.enabled": True})
    portal = Portal(polls=None)
    _, messages, _ = await _run(hass, ks, portal)
    assert ks.patches == [{"kiosk.enabled": False}]
    assert "did not come back" in messages[-1]
    assert "Enable Device Owner" in messages[-1]
    assert "Kiosk mode is still off" in messages[-1]


async def test_setup_not_shown_puts_lock_back_and_raises(hass):
    """[KSM-TEST-216] Negative: the setup screen not reaching the front
    raises, the lock setting is restored, and no watch starts."""
    ks = FakeKs({"kiosk.enabled": True, "lockdown.enabled": True})
    portal = Portal(setup_launches=False)
    (a, b, c, d, n), connects = _patches(ks, portal)
    with a, b, c, d, n as notify, pytest.raises(DeviceOwnerError) as err:
        await meta_setup.async_start(hass, _target(), portal)
    assert err.value.code == "meta_setup_failed"
    assert ks.patches == [
        {"kiosk.enabled": False, "lockdown.enabled": False},
        {"kiosk.enabled": True, "lockdown.enabled": True},
    ]
    assert portal.front == KS_ACTIVITY
    notify.assert_not_called()
    assert connects == []


@pytest.mark.parametrize("password,fail", [(None, False), ("pw", True)])
async def test_unusable_settings_api_still_shows_setup(hass, password, fail):
    """[KSM-TEST-216] Negative: no stored password or an unreachable KS API
    doesn't stop Meta setup; the notice tells the user to turn kiosk mode
    off themselves, and nothing is 'restored' later."""
    ks = FakeKs({"kiosk.enabled": True}, fail=fail)
    portal = Portal(polls=2)
    started, messages, _ = await _run(hass, ks, portal, _target(password))
    assert ks.patches == []
    assert portal.front == SETUP_ACTIVITY
    assert "could not be turned off" in started[-1]
    assert "Meta setup is finished" in messages[-1]
    assert "Kiosk mode" not in messages[-1]


async def test_lock_that_cannot_be_restored_is_reported(hass):
    """[KSM-TEST-216] Negative: the Meta login returns but the lock setting
    cannot be turned back on -- the notice says to do it by hand."""
    ks = FakeKs({"kiosk.enabled": True})
    portal = Portal(polls=2)

    real_patch = ks.patch_settings

    async def patch_once(session, host, token, values, *, pin):
        if values.get("kiosk.enabled") is True:
            raise meta_setup.KsApiError("rejected")
        return await real_patch(session, host, token, values, pin=pin)

    ks.patch_settings = patch_once
    _, messages, _ = await _run(hass, ks, portal)
    assert "could not be turned back on" in messages[-1]
