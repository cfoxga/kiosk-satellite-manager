"""Pure, conservative conversion of capability evidence into a dry-run plan."""
from __future__ import annotations

from typing import Any

from .oem_recovery import get_recovery_profile


def _step(
    identifier: str,
    classification: str,
    reason: str,
    prerequisites: list[str],
    expected_postcondition: str,
    fallback: str,
    *,
    user_presence_required: bool,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "classification": classification,
        "reason": reason,
        "prerequisites": prerequisites,
        "expected_postcondition": expected_postcondition,
        "fallback": fallback,
        "user_presence_required": user_presence_required,
    }


def build_onboarding_plan(report: dict[str, Any]) -> dict[str, Any]:
    """Return a plan only; this module never emits executable commands.

    Unknown evidence is deliberately a blocker.  This makes a newer Android
    build or an incomplete collector report fail closed rather than selecting
    an invasive recovery path.
    """
    facts = report.get("facts", {})
    platform = facts.get("platform", {})
    management = facts.get("management", {})
    applications = facts.get("applications", {})
    kiosk_satellite = applications.get("kiosk_satellite", {})
    probes = report.get("probes", {})
    collector_inferences = list(report.get("inferences", []))
    steps: list[dict[str, Any]] = []
    blockers: list[str] = []
    derived_inferences: list[str] = []
    destructive_options: list[dict[str, Any]] = []

    sdk = platform.get("sdk")
    if not isinstance(sdk, int):
        blockers.append("sdk_unknown")
        steps.append(_step(
            "identify_android_version", "unsupported", "The Android SDK level is not observed.",
            ["A readable ro.build.version.sdk probe"], "The SDK level is recorded.",
            "Inspect the device locally; do not choose a compatibility workaround automatically.",
            user_presence_required=True,
        ))

    if probes.get("adb_identity", {}).get("status") != "ok":
        blockers.append("adb_privilege_unknown")
        steps.append(_step(
            "confirm_adb_privileges", "on_device_consent", "ADB identity evidence is unavailable.",
            ["An authorized ADB session"], "The plan has a sanitized ADB identity.",
            "Re-authorize ADB on the device screen and request a new plan.", user_presence_required=True,
        ))

    account_count = management.get("account_count")
    if isinstance(account_count, int) and account_count > 0:
        derived_inferences.append("accounts_present")
        steps.append(_step(
            "review_existing_accounts", "on_device_consent", "The device has existing accounts.",
            ["A person with access to the device"], "Account ownership has been reviewed.",
            "Keep the accounts; account removal is never part of this plan.", user_presence_required=True,
        ))

    if management.get("device_owner") is not True:
        blockers.append("device_owner_absent" if management.get("device_owner") is False else "device_owner_unknown")
        # KSM-BEHAVE-032: dpm set-device-owner refuses outright once any account
        # exists (see docs/SPEC/device-management-strategy.md) -- a populated
        # device is ineligible today, not merely gated, and the plan must say so
        # explicitly rather than looking identical to a genuinely empty device.
        eligible_now = account_count == 0 if isinstance(account_count, int) else None
        if management.get("device_owner") is False:
            do_reason = "The device is not enrolled with a device owner."
            if eligible_now is False:
                do_reason = (
                    "The device is not enrolled with a device owner, and existing accounts "
                    "make enrollment ineligible without a reset."
                )
        else:
            do_reason = "Device-owner state is not observed."
        device_owner_step = _step(
            "device_owner_enrollment", "destructive_gated", do_reason,
            ["Explicit owner approval", "Physical access", "A separately approved reset/enrollment procedure"],
            "A device-owner decision has been made outside KSM.",
            "Continue without device-owner features; KSM will not reset or enroll the device.",
            user_presence_required=True,
        )
        steps.append(device_owner_step)
        destructive_options.append({
            "id": "device_owner_enrollment",
            "classification": "destructive_gated",
            "reason": device_owner_step["reason"],
            "requires_explicit_user_consent": True,
            "executor_authorized": False,
            "eligible_now": eligible_now,
        })

    if kiosk_satellite.get("installed") is False:
        steps.append(_step(
            "install_kiosk_satellite", "automatic_with_verification", "Kiosk Satellite is not installed.",
            ["ADB access remains authorized"],
            "Kiosk Satellite is installed and its health endpoint responds.",
            "Use the Install/Reinstall Kiosk Satellite button after resolving the reported blocker.",
            user_presence_required=False,
        ))
    elif kiosk_satellite.get("installed") is True:
        steps.append(_step(
            "validate_existing_kiosk_satellite", "automatic_reversible", "Kiosk Satellite is already installed.",
            ["The existing installation remains reachable"], "The installed version is reported.",
            "Use the existing installation unchanged; reinstall remains a separate user action.",
            user_presence_required=False,
        ))
    else:
        blockers.append("kiosk_satellite_state_unknown")

    # KSM-BEHAVE-038, cfoxga/kiosk-satellite-manager#12 Phases 2-4: the
    # Test Harness reset is a standing "if ordinary ADB provisioning cannot
    # reach the required state" recovery option, gated by the OEM recovery
    # profile's live-confirmed status -- never by this plan guessing.
    # Present in every plan (Acceptance: "Unsupported OEM behavior produces a
    # complete support report"), eligible only when confirmed.
    recovery_profile = get_recovery_profile(platform.get("device_profile_key"))
    test_harness_confirmed = recovery_profile.test_harness_confirmed
    test_harness_eligible = test_harness_confirmed is True
    steps.append(_step(
        "test_harness_reset", "destructive_gated",
        recovery_profile.test_harness_notes,
        [
            "Explicit destructive checkpoint confirmed by a human",
            "The operating host's ADB key is already trusted by the device",
            "The device is kept fully offline after the wipe until enrollment/setup completes",
        ],
        (
            "; ".join(recovery_profile.postconditions)
            if recovery_profile.postconditions
            else "A person has confirmed the device's post-reset state locally."
        ),
        "Do not reset; continue with the device in its current state.",
        user_presence_required=True,
    ))
    destructive_options.append({
        "id": "test_harness_reset",
        "classification": "destructive_gated",
        "reason": recovery_profile.test_harness_notes,
        "requires_explicit_user_consent": True,
        "executor_authorized": False,
        "eligible_now": test_harness_eligible,
        "data_loss": [
            "All accounts, apps, and settings on the device",
            "Any data not preserved by the persistent data block (the trusted "
            "ADB key itself does survive the wipe)",
        ],
        "rollback_notes": recovery_profile.rollback_notes,
    })
    if not test_harness_eligible:
        blockers.append(
            "test_harness_not_confirmed" if test_harness_confirmed is None
            else "test_harness_known_unsupported"
        )

    # Acceptance: "Destructive and vulnerability-based options require
    # distinct explicit authorization" -- kept as its own entry, never
    # folded into test_harness_reset above, and permanently unsupported
    # until a vulnerability-based recovery is itself live-verified
    # (KSM-OPEN-005). This module never invokes an exploit.
    destructive_options.append({
        "id": "vulnerability_based_cleanup",
        "classification": "unsupported",
        "reason": (
            "No vulnerability-based (non-wipe) account-cleanup recipe has "
            "been live-verified for any device profile (KSM-OPEN-005)."
        ),
        "requires_explicit_user_consent": True,
        "executor_authorized": False,
        "eligible_now": False,
    })

    return {
        "schema_version": 1,
        "summary": f"{len(steps)} planned step(s), {len(blockers)} blocker(s), no action executed.",
        "facts": facts,
        "collector_inferences": collector_inferences,
        "derived_inferences": derived_inferences,
        "steps": steps,
        "blockers": blockers,
        # Planner output cannot be passed to an executor. Future executors
        # must make their own authorization decision.
        "automatic_actions": [],
        "destructive_options": destructive_options,
        "oem_recovery": {
            "profile_key": recovery_profile.key,
            "test_harness_confirmed": recovery_profile.test_harness_confirmed,
            "test_harness_notes": recovery_profile.test_harness_notes,
            "restrictions": list(recovery_profile.restrictions),
            "rollback_notes": recovery_profile.rollback_notes,
            "vulnerability_recovery_available": recovery_profile.vulnerability_recovery_available,
        },
    }
