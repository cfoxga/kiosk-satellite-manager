"""KSM-TEST-167..169 (cfoxga/kiosk-satellite-manager#54): opt-in Device Owner
preflight and enrollment against a scripted ADB shell.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.kiosk_satellite_manager import device_owner
from custom_components.kiosk_satellite_manager.device_owner import (
    DeviceOwnerError,
    enable_device_owner,
    run_preflight,
)
from custom_components.kiosk_satellite_manager.device_catalog import require_recipe

from ksm_device_owner_fake import (  # noqa: E402
    KS_ACTIVITY, KS_ADMIN, META, SECRET, SETUP, SETUP_ACTIVITY, _META_TYPES, FakeDevice,
)


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


class _Android9CleanupDevice(FakeDevice):
    """Readbacks move only when the confirmed cleanup issues each command."""

    def __init__(self, *, adb_enabled=True, home_ks=True, sdk="28", product="aloha",
                 manufacturer="Facebook", model="Portal",
                 port="5555", missing_package=None, clear_fails=False,
                 disable_fails=False, report_disabled=True, accounts_return=False,
                 owner_lost=False, adb_lost=False):
        super().__init__(restore_readds_hw=True)
        self.adb_enabled = adb_enabled
        self.home_ks = home_ks
        self.disabled: set[str] = set()
        self.cleaned: set[str] = set()
        self.sdk = sdk
        self.product = product
        self.manufacturer = manufacturer
        self.model = model
        self.port = port
        self.missing_package = missing_package
        self.clear_fails = clear_fails
        self.disable_fails = disable_fails
        self.report_disabled = report_disabled
        self.accounts_return = accounts_return
        self.owner_lost = owner_lost
        self.adb_lost = adb_lost

    async def shell(self, command: str) -> str:
        if command == "getprop ro.build.version.sdk":
            self.commands.append(command)
            return self.sdk
        if command == "getprop ro.product.device":
            self.commands.append(command)
            return self.product
        if command == "getprop ro.product.manufacturer":
            self.commands.append(command)
            return self.manufacturer
        if command == "getprop ro.product.model":
            self.commands.append(command)
            return self.model
        if command == "getprop service.adb.tcp.port":
            self.commands.append(command)
            return self.port
        if command == "settings get global adb_enabled":
            self.commands.append(command)
            return "1" if self.adb_enabled and not (self.adb_lost and self.disabled) else "0"
        if command == "dumpsys account" and self.accounts_return and self.disabled:
            self.account_types = ["com.facebook.aloha.hw"]
        if command == "dumpsys device_policy" and self.owner_lost and self.disabled:
            self.owner = None
        if command.startswith("pm path "):
            self.commands.append(command)
            if command[8:] == self.missing_package:
                return ""
            return f"package:/system/app/{command[8:]}/base.apk"
        if command.startswith("pm clear "):
            self.commands.append(command)
            package = command[9:]
            if self.clear_fails:
                return "Failed"
            self.cleaned.add(package)
            if package == META:
                self.account_types = []
            return "Success"
        if command.startswith("pm disable-user --user 0 "):
            self.commands.append(command)
            if self.disable_fails:
                return "Error"
            self.disabled.add(command.split()[-1])
            return "new state: disabled-user"
        if command == "pm list packages -d":
            self.commands.append(command)
            return "\n".join(f"package:{p}" for p in sorted(self.disabled)) if self.report_disabled else ""
        if command.startswith("cmd package resolve-activity --brief"):
            self.commands.append(command)
            return "me.jxl.kiosk_satellite/.HomeAlias" if self.home_ks else "com.facebook.alohaapps.launcher/.HomeActivity"
        if command == "echo ksm_adb_ready":
            self.commands.append(command)
            return "ksm_adb_ready"
        return await super().shell(command)


async def test_android9_cleanup_requires_confirmation_and_native_home():
    """[KSM-TEST-267] No package or owner mutation before confirmation/readiness."""
    recipe = require_recipe("portal_gen1")
    for confirmed, home in ((False, True), (True, False)):
        dev = _Android9CleanupDevice()
        with pytest.raises(DeviceOwnerError):
            await device_owner.repurpose_android9_portal(
                dev, "portal_gen1", recipe, confirmed=confirmed, ks_home_enabled=home
            )
        assert dev.mutations() == [] and dev.cleaned == set() and dev.disabled == set()


async def test_android9_cleanup_sets_owner_clears_meta_and_preserves_adb():
    """[KSM-TEST-267] Confirmation yields verified owner, clean accounts and ADB."""
    dev = _Android9CleanupDevice()
    result = await device_owner.repurpose_android9_portal(
        dev, "portal_gen1", require_recipe("portal_gen1"),
        confirmed=True, ks_home_enabled=True,
    )
    assert result.owner_enabled is True and result.adb_available is True
    assert result.accounts_remaining == 0
    assert dev.owner == "me.jxl.kiosk_satellite"
    assert META in dev.cleaned and META in dev.disabled
    assert "com.facebook.alohaapps.launcher" in dev.disabled
    assert "com.facebook.alohaservices.abilitymanager" in dev.disabled
    assert "com.facebook.alohaapps.settings" not in dev.disabled
    assert "com.facebook.aloha.system.services" not in dev.disabled
    assert "pm clear me.jxl.kiosk_satellite" not in dev.commands


async def test_android9_cleanup_refuses_unqualified_model_and_adb_loss():
    """[KSM-TEST-267] A shared recipe does not qualify a sibling; ADB loss fails."""
    dev = _Android9CleanupDevice()
    with pytest.raises(DeviceOwnerError):
        await device_owner.repurpose_android9_portal(
            dev, "portal_plus_gen1", require_recipe("portal_plus_gen1"),
            confirmed=True, ks_home_enabled=True,
        )
    assert dev.mutations() == []
    dev = _Android9CleanupDevice(adb_enabled=False)
    with pytest.raises(DeviceOwnerError):
        await device_owner.repurpose_android9_portal(
            dev, "portal_gen1", require_recipe("portal_gen1"),
            confirmed=True, ks_home_enabled=True,
        )
    assert dev.mutations() == []


@pytest.mark.parametrize("options,code", [
    ({"sdk": "29"}, "cleanup_unsupported"),
    ({"product": "other"}, "cleanup_unsupported"),
    ({"manufacturer": "Other"}, "cleanup_unsupported"),
    ({"model": "Portal+"}, "cleanup_unsupported"),
    ({"port": "-1"}, "adb_required"),
    ({"missing_package": META}, "package_missing"),
])
async def test_android9_preflight_refuses_changed_hardware_or_adb(options, code):
    """[KSM-TEST-267] Identity and ADB controls prevent any mutation."""
    dev = _Android9CleanupDevice(**options)
    with pytest.raises(DeviceOwnerError) as err:
        await device_owner.repurpose_android9_portal(
            dev, "portal_gen1", require_recipe("portal_gen1"),
            confirmed=True, ks_home_enabled=True,
        )
    assert err.value.code == code and not dev.mutations()
    assert not dev.cleaned and not dev.disabled


async def test_android9_preflight_refuses_another_owner():
    dev = _Android9CleanupDevice()
    dev.owner = "com.example.other"
    with pytest.raises(DeviceOwnerError) as err:
        await device_owner.repurpose_android9_portal(
            dev, "portal_gen1", require_recipe("portal_gen1"),
            confirmed=True, ks_home_enabled=True,
        )
    assert err.value.code == "preflight_blocked" and not dev.mutations()


async def test_android9_preflight_requires_ks_as_actual_android_home():
    """[KSM-TEST-267] A KS toggle alone cannot authorize launcher removal."""
    dev = _Android9CleanupDevice(home_ks=False)
    with pytest.raises(DeviceOwnerError) as err:
        await device_owner.repurpose_android9_portal(
            dev, "portal_gen1", require_recipe("portal_gen1"),
            confirmed=True, ks_home_enabled=True,
        )
    assert err.value.code == "ks_home_required"
    assert not dev.mutations() and not dev.cleaned and not dev.disabled


@pytest.mark.parametrize("options", [
    {"clear_fails": True}, {"disable_fails": True},
    {"report_disabled": False},
    {"accounts_return": True}, {"owner_lost": True}, {"adb_lost": True},
])
async def test_android9_cleanup_detects_partial_application(options):
    """[KSM-TEST-267] Command and readback failures never claim cleanup success."""
    dev = _Android9CleanupDevice(**options)
    with pytest.raises(DeviceOwnerError) as err:
        await device_owner.repurpose_android9_portal(
            dev, "portal_gen1", require_recipe("portal_gen1"),
            confirmed=True, ks_home_enabled=True,
        )
    assert err.value.code == "cleanup_partial"


async def test_android9_cleanup_accepts_existing_ks_owner():
    """[KSM-TEST-267] A prior owner enrollment does not cause a second dpm call."""
    dev = _Android9CleanupDevice()
    dev.owner = "me.jxl.kiosk_satellite"
    result = await device_owner.repurpose_android9_portal(
        dev, "portal_gen1", require_recipe("portal_gen1"),
        confirmed=True, ks_home_enabled=True,
    )
    assert result.owner_enabled
    assert not any(c.startswith("dpm set") for c in dev.commands)


@pytest.mark.parametrize("missing", [
    "com.facebook.aloha.state", "com.facebook.aloha.app.storytime",
])
async def test_android9_cleanup_skips_absent_optional_meta_packages(missing):
    """[KSM-TEST-267] Optional app variants do not block audited cleanup."""
    dev = _Android9CleanupDevice(missing_package=missing)
    result = await device_owner.repurpose_android9_portal(
        dev, "portal_gen1", require_recipe("portal_gen1"),
        confirmed=True, ks_home_enabled=True,
    )
    assert result.owner_enabled and missing not in result.cleared_packages
    assert missing not in result.disabled_packages


@pytest.mark.parametrize("model_key,types", [
    # An account registered by a package no model lists.
    ("portal_mini", ("com.facebook.aloha.sso", "com.example.other")),
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


IDENTITY = ("com.facebook.aloha.pl", "com.facebook.aloha.privowner", "com.facebook.aloha.sso")


@pytest.mark.parametrize("model_key,types,missing", [
    ("portal_mini", tuple(f"com.facebook.aloha.{t}" for t in _META_TYPES), ()),
    # What the purge leaves behind: only the hardware account comes back.
    ("portal_go", ("com.facebook.aloha.hw",), IDENTITY),
    ("portal_mini", (), IDENTITY),
    # Models without a Meta setup app never report a missing identity.
    ("portal_gen2", (), IDENTITY),
    (None, (), ()),
])
async def test_preflight_reports_missing_meta_identity(model_key, types, missing):
    """[KSM-TEST-214] The preflight names the Meta login account types
    missing on a Portal with a Meta setup app, and nothing elsewhere."""
    dev = FakeDevice(account_types=types)
    pre = await run_preflight(dev, model_key)
    assert pre.meta_identity_missing == missing
    assert dev.mutations() == []


async def test_portal_go_clears_meta_accounts():
    """[KSM-TEST-214] PortalGo has live purge/restore evidence (2026-09-28):
    its Meta accounts are clearable, and enrollment flags Meta setup."""
    dev = FakeDevice(restore_readds_hw=True)
    pre = await run_preflight(dev, "portal_go")
    assert pre.ready and pre.clear_packages == (META,)
    result = await enable_device_owner(dev, "portal_go")
    assert result.meta_setup_needed is True
    # Only the hardware account came back by itself -- the identity is gone.
    assert await device_owner.read_meta_identity_missing(dev) == IDENTITY


async def test_portal_gen2_clears_accounts_and_requests_meta_setup():
    """[KSM-TEST-243] Great Room Gen 2 has the same live account package
    and Device Owner sequence; an unlisted Portal remains blocked above."""
    dev = FakeDevice(restore_readds_hw=True)
    pre = await run_preflight(dev, "portal_gen2")
    assert pre.ready and pre.clear_packages == (META,)
    result = await enable_device_owner(dev, "portal_gen2")
    assert dev.mutations() == [
        f"pm uninstall -k --user 0 {META}",
        f"dpm set-device-owner {KS_ADMIN}",
        f"cmd package install-existing --user 0 {META}",
    ]
    assert result.meta_setup_needed is True
    assert dev.owner == "me.jxl.kiosk_satellite"


async def test_portal_plus_gen2_clears_accounts_and_shows_meta_setup():
    """[KSM-TEST-334] Basement Office Portal+ Gen 2 has the Gen 2 account
    shape (2026-10-03 read-only ADB): it clears through alohausers, flags
    Meta setup, reports a missing identity, and can relaunch Meta setup."""
    dev = FakeDevice(restore_readds_hw=True)
    pre = await run_preflight(dev, "portal_plus_gen2")
    assert pre.ready and pre.clear_packages == (META,)
    assert pre.meta_identity_missing == ()
    result = await enable_device_owner(dev, "portal_plus_gen2")
    assert dev.mutations() == [
        f"pm uninstall -k --user 0 {META}",
        f"dpm set-device-owner {KS_ADMIN}",
        f"cmd package install-existing --user 0 {META}",
    ]
    assert result.meta_setup_needed is True
    assert dev.owner == "me.jxl.kiosk_satellite"
    assert await device_owner.read_meta_identity_missing(dev) == IDENTITY
    await device_owner.restart_meta_setup(dev, "portal_plus_gen2")
    assert dev.front == SETUP_ACTIVITY


@pytest.mark.parametrize("model_key,types", [
    # A foreign authenticator still blocks on the newly listed model.
    ("portal_plus_gen2", ("com.facebook.aloha.sso", "com.example.other")),
    # Its Android 9 sibling stays off the Android 10 alohausers path.
    ("portal_plus_gen1", tuple(f"com.facebook.aloha.{t}" for t in _META_TYPES)),
])
async def test_portal_plus_negatives_block_without_mutation(model_key, types):
    """[KSM-TEST-334] Negative: listing Portal+ Gen 2 approves only its
    alohausers accounts, and never the Gen 1 Portal+ Meta setup path."""
    dev = FakeDevice(account_types=types)
    pre = await run_preflight(dev, model_key)
    assert "accounts_blocked" in pre.blockers and pre.ready is False
    with pytest.raises(DeviceOwnerError) as err:
        await device_owner.restart_meta_setup(dev, "portal_plus_gen1")
    assert err.value.code == "meta_setup_unsupported"
    assert dev.mutations() == []


@pytest.mark.parametrize("model_key,types,needed", [
    ("portal_mini", tuple(f"com.facebook.aloha.{t}" for t in _META_TYPES), True),
    # Nothing cleared, but the identity is already gone: setup still needed.
    ("portal_go", (), True),
    # No verified Meta setup app on this model.
    ("portal_gen1", (), False),
])
async def test_enrollment_flags_meta_setup(model_key, types, needed):
    """[KSM-TEST-214] meta_setup_needed follows the model and whether the
    Meta identity is gone after enrollment; enrollment itself never
    touches Meta setup."""
    dev = FakeDevice(account_types=types)
    result = await enable_device_owner(dev, model_key)
    assert result.meta_setup_needed is needed
    assert not any(SETUP in c for c in dev.commands)


@pytest.mark.parametrize("model_key", ["portal_go", "portal_gen2"])
async def test_restart_meta_setup_resets_and_launches(model_key):
    """[KSM-TEST-215/243] Go and Gen 2 reset and launch Meta setup; success
    requires the setup screen actually in front."""
    dev = FakeDevice(owner="me.jxl.kiosk_satellite")
    await device_owner.restart_meta_setup(dev, model_key)
    assert dev.mutations() == [
        f"pm uninstall -k --user 0 {SETUP}",
        f"cmd package install-existing --user 0 {SETUP}",
        f"am start -n {SETUP_ACTIVITY}",
    ]
    assert f"pm list packages -e --user 0 {SETUP}" in dev.commands
    assert dev.front == SETUP_ACTIVITY
    assert dev.commands[-1] == "dumpsys activity activities"


async def test_restart_meta_setup_restore_failure_names_package():
    """[KSM-TEST-215] Negative: setup app not coming back is restore_failed
    naming the package and its restore command; nothing is launched."""
    dev = FakeDevice(setup_restore_works=False)
    with pytest.raises(DeviceOwnerError) as err:
        await device_owner.restart_meta_setup(dev, "portal_mini")
    assert err.value.code == "restore_failed"
    assert SETUP in str(err.value) and "install-existing" in str(err.value)
    assert not any(c.startswith("am start") for c in dev.commands)


async def test_restart_meta_setup_fails_when_screen_stays_hidden():
    """[KSM-TEST-215] Negative: am start 'succeeding' while Kiosk Satellite
    stays in front (a kiosk lock) is meta_setup_failed, not success."""
    dev = FakeDevice(setup_launches=False)
    with pytest.raises(DeviceOwnerError) as err:
        await device_owner.restart_meta_setup(dev, "portal_mini")
    assert err.value.code == "meta_setup_failed"
    assert dev.front == KS_ACTIVITY


async def test_restart_meta_setup_refuses_unsupported_model():
    """[KSM-TEST-215] Negative: a model without a known Meta setup app gets
    no command at all."""
    dev = FakeDevice()
    with pytest.raises(DeviceOwnerError) as err:
        await device_owner.restart_meta_setup(dev, "portal_gen1")
    assert err.value.code == "meta_setup_unsupported"
    assert dev.commands == []


async def test_meta_identity_unreadable_is_none():
    """[KSM-TEST-214] Negative: an unreadable account dump is None, never
    'nothing missing'."""

    class Blank(FakeDevice):
        async def shell(self, command: str) -> str:
            return ""

    assert await device_owner.read_meta_identity_missing(Blank()) is None


@pytest.mark.parametrize("override,detail", [
    ({f"pm uninstall -k --user 0 {SETUP}": "Failure [DELETE_FAILED_INTERNAL_ERROR]\n"},
     "could not reset"),
    ({f"pm list packages -e --user 0 {SETUP}": ""}, "still disabled"),
    ({f"am start -n {SETUP_ACTIVITY}": "Error: Activity not started\n"}, "could not be started"),
    ({"dumpsys activity activities": "no focus lines here\n"}, "did not come to the front"),
])
async def test_restart_meta_setup_step_failures(override, detail):
    """[KSM-TEST-215] Negative: each step's refusal is meta_setup_failed
    with its own reason, never success."""

    class Scripted(FakeDevice):
        async def shell(self, command: str) -> str:
            if command in override:
                self.commands.append(command)
                return override[command]
            return await super().shell(command)

    dev = Scripted()
    with pytest.raises(DeviceOwnerError) as err:
        await device_owner.restart_meta_setup(dev, "portal_mini")
    assert err.value.code == "meta_setup_failed"
    assert detail in err.value.detail

_PKG = Path(__file__).resolve().parents[2] / "custom_components" / "kiosk_satellite_manager"


def _texts(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _texts(value)
    elif isinstance(node, str):
        yield node


def test_meta_login_text_offers_facebook_and_whatsapp():
    """[KSM-TEST-217] A Meta Portal signs in with Facebook or WhatsApp, so
    no dialog may tell the user WhatsApp is the only way (#54)."""
    strings = json.loads((_PKG / "strings.json").read_text())
    whatsapp = [t for t in _texts(strings) if "WhatsApp" in t]
    assert whatsapp, "positive control: the Meta setup text names WhatsApp"
    assert [t for t in whatsapp if "Facebook" not in t] == []



def test_KSM_TEST_346_no_reset_screen_remains():
    """[KSM-TEST-346] KSM-BEHAVE-172 is retired: KSM has no way to open or
    send a factory reset."""
    from custom_components.kiosk_satellite_manager import support_log
    assert hasattr(device_owner, "owner_package"), "positive control: module is the real one"
    assert not hasattr(device_owner, "FACTORY_RESET_SCREENS")
    assert not hasattr(device_owner, "open_factory_reset_screen")
    assert "factory_reset_screen" not in support_log.KINDS
    assert "repair" not in support_log.SOURCES
