"""KSM-TEST-167..169 (cfoxga/kiosk-satellite-manager#54): opt-in Device Owner
preflight and enrollment against a scripted ADB shell.
"""
from __future__ import annotations

import pytest

from custom_components.kiosk_satellite_manager import device_owner
from custom_components.kiosk_satellite_manager.device_owner import (
    DeviceOwnerError,
    enable_device_owner,
    run_preflight,
)

from ksm_device_owner_fake import FakeDevice, KS_ADMIN, META, SECRET, _META_TYPES  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(device_owner, "_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(device_owner, "_POLL_ATTEMPTS", 3)


async def test_preflight_parses_types_packages_and_never_names():
    """[KSM-TEST-167] Account types, their registering packages, users and
    owner come from dumpsys; no account name survives into the result."""
    dev = FakeDevice()
    pre = await run_preflight(dev, "portal_mini")
    assert pre.ks_installed is True
    assert pre.owner_package is None
    assert pre.user_count == 1
    assert pre.account_counts == {f"com.facebook.aloha.{t}": 1 for t in _META_TYPES}
    assert pre.account_packages == {META}
    assert pre.clear_packages == (META,)
    assert pre.blockers == ()
    assert pre.ready is True
    assert SECRET not in repr(pre)
    assert dev.mutations() == []


@pytest.mark.parametrize("model_key,types", [
    # An account registered by a package no model lists.
    ("portal_mini", ("com.facebook.aloha.sso", "com.example.other")),
    # The Meta package, but on a model with no live evidence for clearing it.
    ("portal_go", ("com.facebook.aloha.sso",)),
    ("portal_gen2", ("com.facebook.aloha.sso",)),
    (None, ("com.facebook.aloha.sso",)),
    # An account type with no registered authenticator at all.
    ("portal_mini", ("com.unknown.type",)),
])
async def test_preflight_blocks_unlisted_account_owner(model_key, types):
    """[KSM-TEST-167] Negative: an account whose registering package isn't
    listed for this exact model blocks, and enrollment mutates nothing."""
    dev = FakeDevice(account_types=types)
    pre = await run_preflight(dev, model_key)
    assert "accounts_blocked" in pre.blockers
    assert pre.ready is False
    with pytest.raises(DeviceOwnerError) as err:
        await enable_device_owner(dev, model_key)
    assert err.value.code == "preflight_blocked"
    assert dev.mutations() == []


async def test_enrollment_clears_sets_restores_in_order():
    """[KSM-TEST-168] uninstall -> set-device-owner -> install-existing, and
    success comes from the device_policy readback."""
    dev = FakeDevice()
    result = await enable_device_owner(dev, "portal_mini")
    assert dev.mutations() == [
        f"pm uninstall -k --user 0 {META}",
        f"dpm set-device-owner {KS_ADMIN}",
        f"cmd package install-existing --user 0 {META}",
    ]
    assert result.cleared_packages == (META,)
    assert dev.owner == "me.jxl.kiosk_satellite"
    assert dev.meta_installed is True
    # Readback happened after set-device-owner, not only before it.
    last_set = dev.commands.index(f"dpm set-device-owner {KS_ADMIN}")
    assert "dumpsys device_policy" in dev.commands[last_set + 1:]
    assert SECRET not in repr(result)


@pytest.mark.parametrize("kwargs,code", [
    ({"set_owner_output": "java.lang.IllegalStateException: Not allowed",
      "set_owner_takes_effect": False}, "set_owner_failed"),
    ({"uninstall_purges": False}, "accounts_remain"),
])
async def test_enrollment_failure_still_restores_package(kwargs, code):
    """[KSM-TEST-168] Negative: a failed step still restores the removed
    package, and no success is reported."""
    dev = FakeDevice(**kwargs)
    with pytest.raises(DeviceOwnerError) as err:
        await enable_device_owner(dev, "portal_mini")
    assert err.value.code == code
    assert dev.mutations()[-1] == f"cmd package install-existing --user 0 {META}"
    assert dev.meta_installed is True
    assert SECRET not in str(err.value)
    if code == "accounts_remain":
        assert f"dpm set-device-owner {KS_ADMIN}" not in dev.commands


async def test_success_text_without_readback_is_failure():
    """[KSM-TEST-168] Negative: set-device-owner printing Success while the
    device_policy readback lacks Kiosk Satellite is a failure."""
    dev = FakeDevice(set_owner_takes_effect=False)
    with pytest.raises(DeviceOwnerError) as err:
        await enable_device_owner(dev, "portal_mini")
    assert err.value.code == "readback_failed"
    assert dev.meta_installed is True


async def test_restore_failure_is_reported_even_after_owner_set():
    """[KSM-TEST-168] Negative: a package that does not come back is an error
    naming the package, even though Device Owner itself landed."""
    dev = FakeDevice(restore_works=False)
    with pytest.raises(DeviceOwnerError) as err:
        await enable_device_owner(dev, "portal_mini")
    assert err.value.code == "restore_failed"
    assert META in str(err.value)


async def test_zero_accounts_touches_no_package():
    """[KSM-TEST-169] With no accounts, set-device-owner runs alone, on any
    model -- clearing evidence isn't needed."""
    dev = FakeDevice(account_types=())
    pre = await run_preflight(dev, "portal_go")
    assert pre.ready is True and pre.clear_packages == ()
    await enable_device_owner(dev, "portal_go")
    assert dev.mutations() == [f"dpm set-device-owner {KS_ADMIN}"]


@pytest.mark.parametrize("kwargs,blocker", [
    ({"owner": "me.jxl.kiosk_satellite"}, "already_owner"),
    ({"owner": "com.example.dpc"}, "other_owner"),
    ({"users": 2}, "multiple_users"),
    ({"ks_installed": False}, "ks_missing"),
])
async def test_ineligible_device_is_never_mutated(kwargs, blocker):
    """[KSM-TEST-169] Negative: existing owner, another owner, extra users, or
    Kiosk Satellite missing -> no mutation at all."""
    dev = FakeDevice(**kwargs)
    pre = await run_preflight(dev, "portal_mini")
    assert blocker in pre.blockers
    with pytest.raises(DeviceOwnerError):
        await enable_device_owner(dev, "portal_mini")
    assert dev.mutations() == []


async def test_unreadable_state_blocks():
    """[KSM-TEST-169] Negative: empty dumpsys output is unobserved, not
    'no owner, no accounts' -- it blocks."""

    class Blank(FakeDevice):
        async def shell(self, command: str) -> str:
            self.commands.append(command)
            return ""

    dev = Blank()
    pre = await run_preflight(dev, "portal_mini")
    assert "unobserved" in pre.blockers
    assert dev.mutations() == []


async def test_malformed_owner_block_is_unobserved():
    """[KSM-TEST-169] Negative (review): a Device Owner block the parser cannot
    read is unobserved, never 'no owner' -- it must not reach the uninstall."""

    class Odd(FakeDevice):
        async def shell(self, command: str) -> str:
            if command == "dumpsys device_policy":
                self.commands.append(command)
                return "  Device Owner: \n    admin=<redacted>\n    package=com.example.dpc\n"
            return await super().shell(command)

    dev = Odd()
    pre = await run_preflight(dev, "portal_mini")
    assert "unobserved" in pre.blockers
    with pytest.raises(DeviceOwnerError):
        await enable_device_owner(dev, "portal_mini")
    assert dev.mutations() == []


@pytest.mark.parametrize("row,blocked", [
    # Trailing field after type: still counted, still allowlisted.
    ("    Account {name=%s, type=com.facebook.aloha.sso, flags=1}\n", False),
    # A row whose type cannot be read fails closed.
    ("    Account {name=%s}\n", True),
])
async def test_unusual_account_rows_are_never_invisible(row, blocked):
    """[KSM-TEST-167] Negative (review): every 'Account {' row is accounted
    for; an unreadable one blocks instead of vanishing from the checks."""

    class Rows(FakeDevice):
        async def shell(self, command: str) -> str:
            out = await super().shell(command)
            if command == "dumpsys account" and not blocked and self.meta_installed:
                out = out.replace("  Accounts: 1\n", "  Accounts: 2\n" + row % SECRET, 1)
            if command == "dumpsys account" and blocked:
                out = out.replace("  Accounts: 0\n", "  Accounts: 1\n" + row % SECRET, 1)
            return out

    dev = Rows(account_types=() if blocked else ("com.facebook.aloha.sso",))
    pre = await run_preflight(dev, "portal_mini")
    assert ("accounts_blocked" in pre.blockers) is blocked
    assert SECRET not in repr(pre)
    if not blocked:
        assert pre.account_counts == {"com.facebook.aloha.sso": 2}
        await enable_device_owner(dev, "portal_mini")
        assert dev.owner == "me.jxl.kiosk_satellite"


async def test_transport_drop_during_restore_names_package():
    """[KSM-TEST-168] Negative (review): the connection dies after the package
    was removed and again during restore -> restore_failed naming it."""

    class Drop(FakeDevice):
        async def shell(self, command: str) -> str:
            if command.startswith("dpm set") or command.startswith("cmd package install"):
                self.commands.append(command)
                raise OSError("connection reset")
            return await super().shell(command)

    dev = Drop()
    with pytest.raises(DeviceOwnerError) as err:
        await enable_device_owner(dev, "portal_mini")
    assert err.value.code == "restore_failed"
    assert META in str(err.value)
