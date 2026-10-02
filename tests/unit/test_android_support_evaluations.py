"""KSM-TEST-029: Android device-support skill forward-test scenarios.

Grounds `docs/developer/android-support/evaluations.md` in real collector/planner
behavior rather than narrative assertions. Each scenario below is a fixture the
skill's evaluation doc walks through by hand; the ones with an executable
safe-action contract (device-owner state, SDK-gated device-profile match, and
probe-status vs. derived-fact divergence) are pinned here so a future change to
`capability_report.py`, `onboarding_plan.py`, or the device catalog cannot
silently make the documented guidance wrong without a red test.

Root-observed (scenario 2) and legacy-Device-Admin (scenario 4) are asserted as
*negative* controls: the planner must not treat either as license to skip the
device-owner-absent blocker. Android-10+ Test Harness candidacy (scenario 6) has
no probe in the collector at all yet -- documented as a known evidence gap in
the eval doc, not asserted here, and tracked as its own follow-up issue.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.kiosk_satellite_manager.capability_report import CapabilityReportCollector
from custom_components.kiosk_satellite_manager.device_models import DeviceFacts, match_device_model
from custom_components.kiosk_satellite_manager.onboarding_plan import build_onboarding_plan

def _report(
    *,
    device_owner: bool | None,
    adb_uid: int,
    installed: bool,
    kiosk_satellite_package_status: str = "unsupported",
) -> dict:
    return {
        "schema_version": 2,
        "catalog": {
            "model_key": "portal_go", "model_name": "Meta Portal Go",
            "classification": None, "recipe_key": "meta_portal_android10_local_dns",
            "assignment_state": "approved",
            "support_state": "recipe_assigned",
            "reason": "an approved recipe assignment applies",
            "executable_recipe": True,
        },
        "facts": {
            "platform": {"manufacturer": "Facebook", "model": "PortalGo", "sdk": 29, "device_model_key": "portal_go"},
            "management": {"account_count": 0, "device_owner": device_owner, "adb_uid": adb_uid, "user_count": 1},
            "applications": {"kiosk_satellite": {"installed": installed, "version": None}},
            "oem": {"bootloader_locked": None},
        },
        "probes": {
            "adb_identity": {"status": "ok"},
            "kiosk_satellite_package": {"status": kiosk_satellite_package_status},
        },
        "inferences": [] if installed else ["kiosk_satellite_not_installed"],
    }


def test_scenario_1_stock_non_root_plans_install_with_no_destructive_step():
    """Stock non-root: shell-uid ADB, no device owner, KS absent."""
    plan = build_onboarding_plan(_report(device_owner=False, adb_uid=2000, installed=False))

    assert plan["automatic_actions"] == []
    install = next(step for step in plan["steps"] if step["id"] == "install_kiosk_satellite")
    assert install["classification"] == "automatic_with_verification"
    assert install["user_presence_required"] is False


def test_package_probe_failure_blocks_install_when_package_state_is_unknown():
    """KSM-TEST-048: a denied/error `pm path` result is unknown, not absent."""
    for status in ("denied", "error"):
        plan = build_onboarding_plan(_report(
            device_owner=False,
            adb_uid=2000,
            installed=False,
            kiosk_satellite_package_status=status,
        ))

        assert "package_state_unknown" in plan["blockers"]
        assert all(step["id"] != "install_kiosk_satellite" for step in plan["steps"])


def test_scenario_2_root_observed_adb_never_unlocks_the_owner_blocker():
    """Root-observed (adbd already uid=0, never probed via su) still requires
    an explicit device-owner decision -- an elevated ADB identity is evidence,
    not authorization, for anything past the point non-invasive probing found it."""
    rooted = build_onboarding_plan(_report(device_owner=False, adb_uid=0, installed=False))
    shell = build_onboarding_plan(_report(device_owner=False, adb_uid=2000, installed=False))

    assert rooted["blockers"] == shell["blockers"] == ["device_owner_absent"]
    assert rooted["destructive_options"] == shell["destructive_options"]
    assert rooted["automatic_actions"] == []


async def test_scenario_2_collector_never_shells_out_to_su():
    """Non-invasive privilege detection: the fixed probe allowlist never includes su."""
    client = AsyncMock()
    client.shell = AsyncMock(return_value="")

    await CapabilityReportCollector(client).collect()

    commands = [call.args[0] for call in client.shell.await_args_list]
    assert not any(cmd.strip().startswith("su") or " su " in f" {cmd} " for cmd in commands)


def test_scenario_3_existing_device_owner_clears_the_enrollment_blocker():
    plan = build_onboarding_plan(_report(device_owner=True, adb_uid=2000, installed=True))

    assert "device_owner_absent" not in plan["blockers"]
    assert "device_owner_unknown" not in plan["blockers"]
    # device_owner_enrollment drops out once owned, but test_harness_reset and
    # vulnerability_based_cleanup are unconditional -- every plan carries them
    # (Acceptance: "unsupported OEM behavior produces a complete support report").
    assert [o["id"] for o in plan["destructive_options"]] == ["test_harness_reset", "vulnerability_based_cleanup"]
    assert all(o["executor_authorized"] is False for o in plan["destructive_options"])


def test_scenario_4_legacy_device_admin_with_no_owner_still_fails_closed():
    """capability_report has no dedicated legacy-Device-Admin probe (only
    `dpm get-device-owner`); a device with an active legacy admin but no
    Device Owner reports device_owner=False, same as a fully unmanaged
    device -- the planner must not distinguish (i.e. must not relax) either
    way, matching the documented fail-closed guidance for unmodeled evidence."""
    plan = build_onboarding_plan(_report(device_owner=False, adb_uid=2000, installed=True))

    assert plan["blockers"] == ["device_owner_absent"]
    assert plan["destructive_options"][0]["executor_authorized"] is False


def test_scenario_5_portal_android9_matches_gen1_not_gen2():
    gen1 = match_device_model(DeviceFacts(manufacturer="Facebook", model="Portal", sdk=28))
    gen2 = match_device_model(DeviceFacts(manufacturer="Facebook", model="Portal", sdk=29))

    assert gen1.model_key == "portal_gen1"
    assert gen2.model_key == "portal_gen2"
    assert gen1.model_key != gen2.model_key


async def test_scenario_7_policy_blocked_package_probe_is_distinguishable_from_absent():
    """A `pm path` denial (policy-blocked) and a clean 'not installed' result
    both leave `applications.kiosk_satellite.installed = False`, but only the
    denial leaves a non-"ok"/"unsupported" probe status -- report-integration.md
    tells the skill to check `probes.kiosk_satellite_package.status` before
    treating `installed: false` as "safe to auto-install", exactly because the
    two cases are otherwise indistinguishable from `facts` alone."""
    client_denied = AsyncMock()
    client_denied.shell = AsyncMock(side_effect=[
        "Facebook", "PortalGo", "29", "", "", "",
        "Facebook", "portalgo", "", "", "arm64-v8a",
        "uid=2000(shell)", "",
        "UserInfo{0:Owner:13}", "No device owner", "", "", "", "", "",
        PermissionError("policy blocked"), "",
    ])
    client_absent = AsyncMock()
    client_absent.shell = AsyncMock(side_effect=[
        "Facebook", "PortalGo", "29", "", "", "",
        "Facebook", "portalgo", "", "", "arm64-v8a",
        "uid=2000(shell)", "",
        "UserInfo{0:Owner:13}", "No device owner", "", "", "", "", "",
        "", "",
    ])

    denied = await CapabilityReportCollector(client_denied).collect()
    absent = await CapabilityReportCollector(client_absent).collect()

    assert denied["facts"]["applications"]["kiosk_satellite"]["installed"] is False
    assert absent["facts"]["applications"]["kiosk_satellite"]["installed"] is False
    assert denied["probes"]["kiosk_satellite_package"]["status"] == "denied"
    assert absent["probes"]["kiosk_satellite_package"]["status"] == "unsupported"
    assert denied["probes"]["kiosk_satellite_package"]["status"] != absent["probes"]["kiosk_satellite_package"]["status"]
