"""KSM-TEST-019: capability reports expose only sanitized evidence.

KSM-TEST-034..039 (cfoxga/kiosk-satellite-manager#12): Test Harness
detection facts and the OEM recovery entries `onboarding_plan` derives from
them. KSM-TEST-040 (cfoxga/kiosk-satellite-manager#15): the legacy
`ro.test_harness` signal, surfaced separately as `facts.oem.test_harness_mode`.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.kiosk_satellite_manager.capability_report import CapabilityReportCollector
from custom_components.kiosk_satellite_manager.onboarding_plan import build_onboarding_plan


def _catalog_block(
    model_key: str | None = "portal_go", *, executable: bool = True
) -> dict:
    """The schema-2 `catalog` block a collector emits for a matched model with
    an approved recipe assignment (issue #20). Shaped exactly like
    `CatalogResolution.as_report()`, whose own derivation is tested against the
    real catalog in test_device_catalog.py -- here it is planner input."""
    return {
        "model_key": model_key,
        "model_name": "Meta Portal Go",
        "classification": None if model_key else "unknown",
        "recipe_key": "meta_portal_standard" if executable else None,
        "recipe_version": "v1" if executable else None,
        "assignment_state": "approved" if executable else None,
        "support_state": "recipe_assigned" if executable else "unknown",
        "reason": (
            "an approved recipe assignment applies, with no install-lifecycle "
            "qualification evidence yet"
            if executable
            else "no exact device model matched the observed identity facts"
        ),
        "executable_recipe": executable,
    }


def _observed_report(
    *,
    installed: bool = False,
    device_owner: bool | None = False,
    account_count: int | None = 1,
    device_model_key: str | None = "portal_go",
    catalog: dict | None = None,
) -> dict:
    return {
        "schema_version": 2,
        "catalog": catalog if catalog is not None else _catalog_block(device_model_key),
        "facts": {
            "platform": {
                "manufacturer": "Facebook", "model": "PortalGo", "sdk": 29,
                "characteristics": "", "codename": "portalgo", "fingerprint": None,
                "brand": "Facebook", "product": "portalgo", "board": None,
                "hardware": None, "abi": "arm64-v8a",
                "device_model_key": device_model_key,
                "classification": None if device_model_key else "unknown",
            },
            "management": {
                "account_count": account_count, "device_owner": device_owner,
                "adb_uid": 2000, "user_count": 1, "lockscreen_secure": None,
            },
            "applications": {"kiosk_satellite": {"installed": installed, "version": None}},
            "oem": {"bootloader_locked": True, "test_harness_mode": None},
            "test_harness": {"active": None, "command_supported": None},
        },
        "probes": {"kiosk_satellite_package": {"status": "unsupported"}},
        "inferences": ["kiosk_satellite_not_installed"] if not installed else [],
    }


def test_onboarding_plan_is_deterministic_and_explainable():
    report = _observed_report()

    assert build_onboarding_plan(report) == build_onboarding_plan(report)
    plan = build_onboarding_plan(report)
    assert plan["schema_version"] == 2
    assert plan["facts"] == report["facts"]
    assert plan["collector_inferences"] == ["kiosk_satellite_not_installed"]
    install = next(step for step in plan["steps"] if step["id"] == "install_kiosk_satellite")
    assert install == {
        "id": "install_kiosk_satellite",
        "classification": "automatic_with_verification",
        "reason": "Kiosk Satellite is not installed.",
        "prerequisites": ["ADB access remains authorized"],
        "expected_postcondition": "Kiosk Satellite is installed and its health endpoint responds.",
        "fallback": "Use the Install/Reinstall Kiosk Satellite button after resolving the reported blocker.",
        "user_presence_required": False,
    }


def test_onboarding_plan_never_leaks_destructive_options_into_automatic_actions():
    plan = build_onboarding_plan(_observed_report(device_owner=False))

    assert "device_owner_absent" in plan["blockers"]
    assert plan["automatic_actions"] == []
    assert plan["destructive_options"] == [
        {
            "id": "device_owner_enrollment",
            "classification": "destructive_gated",
            "reason": (
                "The device is not enrolled with a device owner, and existing accounts make "
                "enrollment ineligible without a reset."
            ),
            "requires_explicit_user_consent": True,
            "executor_authorized": False,
            "eligible_now": False,
        },
        {
            "id": "test_harness_reset",
            "classification": "destructive_gated",
            # KSM-BEHAVE-052: portal_go shares meta_portal_standard:v1 with
            # portal_mini, whose Test Harness reset *is* live-confirmed --
            # and still inherits none of that confirmation.
            "reason": "Not live-confirmed on this hardware (KSM-OPEN-003).",
            "requires_explicit_user_consent": True,
            "executor_authorized": False,
            "eligible_now": False,
            "data_loss": [
                "All accounts, apps, and settings on the device",
                "Any data not preserved by the persistent data block (the trusted "
                "ADB key itself does survive the wipe)",
            ],
            "rollback_notes": (
                "No rollback: a Test Harness wipe is destructive and irreversible. "
                "Recovery is re-provisioning from a clean state, not undoing the wipe."
            ),
        },
        {
            "id": "vulnerability_based_cleanup",
            "classification": "unsupported",
            "reason": (
                "No vulnerability-based (non-wipe) account-cleanup recipe has "
                "been live-verified for any device profile (KSM-OPEN-005)."
            ),
            "requires_explicit_user_consent": True,
            "executor_authorized": False,
            "eligible_now": False,
        },
    ]


def test_onboarding_plan_marks_device_owner_eligible_when_no_accounts_present():
    """KSM-BEHAVE-032/KSM-TEST-028: an ineligible populated device must never look

    the same as an eligible empty one -- `dpm set-device-owner` only succeeds
    with zero accounts (see docs/SPEC/device-management-strategy.md).
    """
    plan = build_onboarding_plan(_observed_report(device_owner=False, account_count=0))

    option = plan["destructive_options"][0]
    assert option["eligible_now"] is True
    assert option["reason"] == "The device is not enrolled with a device owner."
    assert option["executor_authorized"] is False


def test_onboarding_plan_marks_device_owner_eligibility_unknown_when_account_count_unobserved():
    plan = build_onboarding_plan(_observed_report(device_owner=False, account_count=None))

    option = plan["destructive_options"][0]
    assert option["eligible_now"] is None
    assert option["reason"] == "The device is not enrolled with a device owner."


def test_onboarding_plan_gates_test_harness_reset_on_recovery_profile_confirmation():
    """KSM-BEHAVE-038: eligibility follows the OEM recovery profile, not a guess."""
    plan = build_onboarding_plan(_observed_report(device_model_key="portal_mini"))

    option = next(o for o in plan["destructive_options"] if o["id"] == "test_harness_reset")
    assert option["eligible_now"] is True
    assert plan["oem_recovery"]["profile_key"] == "portal_mini"
    assert plan["oem_recovery"]["test_harness_confirmed"] is True


def test_onboarding_plan_fails_closed_for_unconfirmed_android_9_portal():
    """KSM-OPEN-003: Gen 1 (Android 9) Portal hardware is never treated as eligible."""
    plan = build_onboarding_plan(_observed_report(device_model_key="portal_gen1"))

    option = next(o for o in plan["destructive_options"] if o["id"] == "test_harness_reset")
    assert option["eligible_now"] is False
    assert "test_harness_not_confirmed" in plan["blockers"]


def test_onboarding_plan_vulnerability_based_cleanup_is_never_eligible():
    """Acceptance: vulnerability-based options stay distinctly, permanently gated."""
    plan = build_onboarding_plan(_observed_report(device_model_key="portal_mini"))

    option = next(o for o in plan["destructive_options"] if o["id"] == "vulnerability_based_cleanup")
    assert option["classification"] == "unsupported"
    assert option["eligible_now"] is False
    assert option["executor_authorized"] is False
    assert plan["oem_recovery"]["vulnerability_recovery_available"] is False


async def test_report_is_sanitized_and_marks_unsupported_probes():
    client = AsyncMock()
    client.shell = AsyncMock(
        side_effect=[
            "Facebook\n", "PortalGo\n", "29\n",
            "\n", "portalgo\n", "Facebook/portalgo/portalgo:10\n",
            # schema 2 (issue #20): brand/product/board/hardware/abi
            "Facebook\n", "portalgo\n", "\n", "\n", "arm64-v8a\n",
            "uid=2000(shell) gid=2000(shell)\n",
            "Account {name=person@example.com, type=com.google}\n",
            "Users:\n\tUserInfo{0:Owner:13} running\n",
            "Unknown command: get-device-owner\n",
            "1\n",
            "1\n",
            "1\n",
            "Unknown command: testharness\n",
            "false\n",
            "package:/data/app/me.jxl.kiosk_satellite/base.apk\n",
            "versionName=2026.9.60\n",
        ]
    )

    report = await CapabilityReportCollector(client).collect()

    assert report["schema_version"] == 2
    assert report["facts"]["platform"] == {
        "manufacturer": "Facebook", "model": "PortalGo", "sdk": 29,
        "characteristics": None, "codename": "portalgo",
        "fingerprint": "Facebook/portalgo/portalgo:10",
        "brand": "Facebook", "product": "portalgo", "board": None,
        "hardware": None, "abi": "arm64-v8a",
        "device_model_key": "portal_go", "classification": None,
    }
    # KSM-TEST-064: the catalog block is derived, and lives outside "facts"
    # precisely so a reader can never mistake it for an observation.
    assert report["catalog"] == {
        "model_key": "portal_go",
        "model_name": "Meta Portal Go",
        "classification": None,
        "recipe_key": "meta_portal_standard",
        "recipe_version": "v1",
        "assignment_state": "approved",
        "support_state": "recipe_assigned",
        "reason": report["catalog"]["reason"],
        "executable_recipe": True,
    }
    # Support is derived from qualification evidence, never claimed: there is
    # no install-lifecycle evidence in the shipped catalog yet, so an
    # assigned, executable recipe still does not read as "supported".
    assert report["catalog"]["support_state"] != "supported"
    assert "no_executable_recipe" not in report["inferences"]
    assert report["facts"]["management"] == {
        "account_count": 1, "device_owner": None, "adb_uid": 2000, "user_count": 1,
        "lockscreen_secure": True,
    }
    assert report["facts"]["oem"] == {"bootloader_locked": True, "test_harness_mode": True}
    assert report["facts"]["test_harness"] == {"active": True, "command_supported": False}
    assert report["facts"]["applications"]["kiosk_satellite"] == {"installed": True, "version": "2026.9.60"}
    assert report["probes"]["device_owner"] == {"status": "unsupported"}
    assert report["probes"]["test_harness_command"] == {"status": "unsupported"}
    assert "person@example.com" not in str(report)
    assert [call.args[0] for call in client.shell.await_args_list] == [
        "getprop ro.product.manufacturer", "getprop ro.product.model",
        "getprop ro.build.version.sdk", "getprop ro.build.characteristics",
        "getprop ro.product.device", "getprop ro.build.fingerprint",
        "getprop ro.product.brand", "getprop ro.product.name",
        "getprop ro.product.board", "getprop ro.hardware",
        "getprop ro.product.cpu.abi",
        "id", "dumpsys account", "pm list users", "dpm get-device-owner",
        "getprop ro.boot.flash.locked", "getprop ro.test_harness",
        "getprop persist.sys.test_harness",
        "cmd testharness get-info", "locksettings get-disabled",
        "pm path me.jxl.kiosk_satellite", "dumpsys package me.jxl.kiosk_satellite",
    ]


async def test_report_sanitizes_denied_probe_errors():
    client = AsyncMock()
    client.shell = AsyncMock(side_effect=PermissionError("private device detail"))

    report = await CapabilityReportCollector(client).collect()

    assert report["facts"]["platform"]["manufacturer"] is None
    assert report["probes"]["manufacturer"] == {"status": "denied", "error": "PermissionError"}
    assert "private device detail" not in str(report)


async def test_report_distinguishes_an_observed_absent_device_owner():
    client = AsyncMock()
    client.shell = AsyncMock(side_effect=[
        "Facebook", "PortalGo", "29", "", "portalgo", "",
        "Facebook", "portalgo", "", "", "arm64-v8a",
        "uid=2000(shell)", "", "UserInfo{0:Owner:13}",
        "No device owner", "1", "0", "0", "", "true", "", "",
    ])

    report = await CapabilityReportCollector(client).collect()

    assert report["facts"]["management"]["device_owner"] is False
    assert report["probes"]["device_owner"] == {"status": "ok"}
    assert report["facts"]["management"]["lockscreen_secure"] is False
    assert report["facts"]["oem"]["test_harness_mode"] is False
    assert report["facts"]["test_harness"] == {"active": False, "command_supported": False}


async def test_report_flags_an_unmatched_device_model():
    """KSM-TEST-065: an unrecognized device is classified, never modeled. The
    fallback classification is reported as a classification, the exact model
    key stays None, and no executable recipe is offered."""
    client = AsyncMock()
    client.shell = AsyncMock(side_effect=[
        "SomeVendor", "SomeModel", "31", "", "somemodel", "",
        "SomeVendor", "somemodel", "", "", "arm64-v8a",
        "uid=2000(shell)", "", "", "", "", "", "", "", "", "", "",
    ])

    report = await CapabilityReportCollector(client).collect()

    assert report["facts"]["platform"]["device_model_key"] is None
    assert report["facts"]["platform"]["classification"] == "generic_android"
    assert "device_model_unmatched" in report["inferences"]
    assert "no_executable_recipe" in report["inferences"]
    assert report["catalog"]["executable_recipe"] is False
    assert report["catalog"]["recipe_key"] is None
    assert report["catalog"]["support_state"] == "unknown"
    assert report["facts"]["oem"]["test_harness_mode"] is None
    assert report["probes"]["test_harness_mode"] == {"status": "unsupported"}


async def test_report_never_lists_a_fallback_classification_as_supported():
    """KSM-TEST-060/065: a Meta-branded device that matches no exact model
    classifies as meta_portal -- a fallback, explicitly *not* an entry on the
    known-supported list, and it gets no recipe from the Portal models that
    share its manufacturer."""
    client = AsyncMock()
    client.shell = AsyncMock(side_effect=[
        "Facebook", "PortalSomethingNew", "33", "nosdcard", "newportal", "",
        "Facebook", "newportal", "", "", "arm64-v8a",
        "uid=2000(shell)", "", "", "", "", "", "", "", "", "", "",
    ])

    report = await CapabilityReportCollector(client).collect()

    assert report["facts"]["platform"]["classification"] == "meta_portal"
    assert report["catalog"]["model_key"] is None
    assert report["catalog"]["executable_recipe"] is False
    assert report["catalog"]["support_state"] == "unknown"


async def test_report_keeps_missing_evidence_out_of_catalog_resolution():
    """KSM-TEST-065: every probe denied means zero observed identity facts.
    A denied probe is missing evidence, not an observation of absence, so the
    resolution must be "unknown" -- never a match on empty strings."""
    client = AsyncMock()
    client.shell = AsyncMock(side_effect=PermissionError("private device detail"))

    report = await CapabilityReportCollector(client).collect()

    assert report["facts"]["platform"]["classification"] == "unknown"
    assert report["catalog"]["model_key"] is None
    assert report["catalog"]["executable_recipe"] is False
    assert all(
        probe["status"] == "denied"
        for probe in report["probes"].values()
    )


def test_onboarding_plan_fails_closed_when_no_recipe_is_executable():
    """KSM-TEST-060: an unassigned or unmatched device gets an explicit
    blocker and an "unsupported" install step -- never the silent Portal
    fallback the pre-catalog code applied to any unmatched device."""
    plan = build_onboarding_plan(
        _observed_report(device_model_key=None, catalog=_catalog_block(None, executable=False))
    )

    assert "no_executable_recipe" in plan["blockers"]
    identify = next(s for s in plan["steps"] if s["id"] == "identify_device_model")
    assert identify["classification"] == "unsupported"
    install = next(s for s in plan["steps"] if s["id"] == "install_kiosk_satellite")
    assert install["classification"] == "unsupported"
    assert plan["automatic_actions"] == []
    assert plan["catalog"]["executable_recipe"] is False


def test_onboarding_plan_recovery_evidence_does_not_follow_a_shared_recipe():
    """KSM-TEST-066: Portal Mini and Portal Go run the same install recipe
    version. Mini's Test Harness reset is live-confirmed; Go's is not, and
    sharing the recipe must not transfer that confirmation. Neither ends up
    in automatic_actions either way."""
    mini = build_onboarding_plan(_observed_report(device_model_key="portal_mini"))
    go = build_onboarding_plan(_observed_report(device_model_key="portal_go"))

    assert mini["catalog"]["recipe_version"] == go["catalog"]["recipe_version"]
    mini_reset = next(o for o in mini["destructive_options"] if o["id"] == "test_harness_reset")
    go_reset = next(o for o in go["destructive_options"] if o["id"] == "test_harness_reset")
    assert mini_reset["eligible_now"] is True
    assert go_reset["eligible_now"] is False
    assert go["oem_recovery"]["test_harness_confirmed"] is None
    assert mini["automatic_actions"] == [] and go["automatic_actions"] == []
