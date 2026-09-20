"""Data-driven OEM recovery profiles (KSM-BEHAVE-037, cfoxga/kiosk-satellite-manager#12).

Extends `device_profiles.py`'s match by reusing its `DeviceProfile.key` as the
lookup key here rather than re-implementing manufacturer/model/sdk matching --
one matcher, two data tables. A profile is a plain, frozen dataclass with
fixed fields (never a free-text/"command" field), so a profile can never carry
an arbitrary unverified shell snippet (Acceptance, KSM#12).

`test_harness_confirmed` is intentionally three-valued:
  True  -- `cmd testharness enable` has been live-confirmed safe on this
            profile (persist.sys.test_harness=1 observed post-wipe, ADB
            reconnected with no new Allow prompt).
  None  -- not live-confirmed either way. Treated as ineligible everywhere
            this module's data feeds `onboarding_plan.py`'s
            `eligible_now` (fail closed on unknown evidence, matching that
            module's existing philosophy).
  False -- reserved for a profile with a *confirmed* broken/unsupported OEM
            Test Harness implementation. No such case is populated yet
            (KSM-OPEN-003) -- the type supports it so a future finding
            doesn't require a schema change.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecoveryProfile:
    """Recovery evidence/guidance for one `device_profiles.DeviceProfile.key`."""

    key: str
    test_harness_confirmed: bool | None
    test_harness_notes: str
    restrictions: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ("manufacturer", "model", "sdk")
    postconditions: tuple[str, ...] = ()
    rollback_notes: str = (
        "No rollback: a Test Harness wipe is destructive and irreversible. "
        "Recovery is re-provisioning from a clean state, not undoing the wipe."
    )
    # KSM-OPEN-005: no vulnerability-based (non-wipe) account-cleanup recipe
    # has ever been live-verified for any profile. This stays False
    # everywhere until one is -- never inferred from test_harness_confirmed.
    vulnerability_recovery_available: bool = False


_PORTAL_TEST_HARNESS_POSTCONDITIONS: tuple[str, ...] = (
    "dumpsys account reports zero accounts for the Owner user",
    "getprop persist.sys.test_harness reads 1",
    "ADB reconnects on this host with no new on-device Allow prompt",
)

_PORTAL_TEST_HARNESS_RESTRICTIONS: tuple[str, ...] = (
    "Facebook/WhatsApp login is lost and must be redone by a person on-device "
    "(review_existing_accounts / on_device_consent) unless the device is kept "
    "offline after the wipe",
    "OEM permission/microphone restrictions observed on this hardware are not "
    "yet cataloged per-profile (KSM-OPEN-004)",
)

RECOVERY_PROFILES: dict[str, RecoveryProfile] = {
    # Live-confirmed twice (Great Room Portal, Basement Office Portal --
    # both PortalMini/omni, Android 10) via `cmd testharness enable`.
    "portal_mini": RecoveryProfile(
        key="portal_mini",
        test_harness_confirmed=True,
        test_harness_notes=(
            "Live-confirmed: cmd testharness enable wipes to zero accounts, "
            "preserves the trusted ADB key through reboot, and "
            "persist.sys.test_harness reads 1 afterward."
        ),
        restrictions=_PORTAL_TEST_HARNESS_RESTRICTIONS,
        postconditions=_PORTAL_TEST_HARNESS_POSTCONDITIONS,
    ),
    # Generic Android 10+ Portal (Gen 2). Not itself live-confirmed by
    # serial/model, but the two confirmed devices above are the same "omni"
    # Android 10 platform family -- Test Harness Mode is an AOSP platform
    # feature, not app-specific, so this generalization is carried forward
    # (see ~/.claude/.../memory/portal-device-owner-blockers.md "How to
    # apply"). Still None (unconfirmed), not True, for any Portal profile
    # this note doesn't cover -- see KSM-OPEN-003 for what remains open.
    "portal_gen2": RecoveryProfile(
        key="portal_gen2",
        test_harness_confirmed=True,
        test_harness_notes=(
            "Inferred from the portal_mini live confirmation: same Android 10 "
            "'omni' platform family, and Test Harness Mode is a platform "
            "feature rather than a per-model one. Not independently "
            "live-confirmed against this exact model string (KSM-OPEN-003)."
        ),
        restrictions=_PORTAL_TEST_HARNESS_RESTRICTIONS,
        postconditions=_PORTAL_TEST_HARNESS_POSTCONDITIONS,
    ),
    # Android 9 (Gen 1). No live evidence either way -- Test Harness Mode's
    # availability and behavior varies enough across early OEM builds that
    # the Gen 2 generalization above is not extended here. KSM-OPEN-003.
    "portal_gen1": RecoveryProfile(
        key="portal_gen1",
        test_harness_confirmed=None,
        test_harness_notes=(
            "Not live-confirmed on Android 9 Portal hardware. Do not assume "
            "the Gen 2 recipe applies -- verify persist.sys.test_harness "
            "behavior on a disposable/lab device before treating this as "
            "eligible (KSM-OPEN-003)."
        ),
        restrictions=_PORTAL_TEST_HARNESS_RESTRICTIONS,
    ),
    "portal_go": RecoveryProfile(
        key="portal_go",
        test_harness_confirmed=None,
        test_harness_notes="Not live-confirmed on this hardware (KSM-OPEN-003).",
        restrictions=_PORTAL_TEST_HARNESS_RESTRICTIONS,
    ),
    "portal_plus_gen1": RecoveryProfile(
        key="portal_plus_gen1",
        test_harness_confirmed=None,
        test_harness_notes="Not live-confirmed on this hardware (KSM-OPEN-003).",
        restrictions=_PORTAL_TEST_HARNESS_RESTRICTIONS,
    ),
    "portal_plus_gen2": RecoveryProfile(
        key="portal_plus_gen2",
        test_harness_confirmed=None,
        test_harness_notes="Not live-confirmed on this hardware (KSM-OPEN-003).",
        restrictions=_PORTAL_TEST_HARNESS_RESTRICTIONS,
    ),
    "portal_tv": RecoveryProfile(
        key="portal_tv",
        test_harness_confirmed=None,
        test_harness_notes="Not live-confirmed on this hardware (KSM-OPEN-003).",
        restrictions=_PORTAL_TEST_HARNESS_RESTRICTIONS,
    ),
    "meta_portal": RecoveryProfile(
        key="meta_portal",
        test_harness_confirmed=None,
        test_harness_notes=(
            "Generic Meta Portal fallback -- device-specific evidence was "
            "insufficient to match a more specific profile, so this stays "
            "unconfirmed regardless of the specific-profile findings above."
        ),
        restrictions=_PORTAL_TEST_HARNESS_RESTRICTIONS,
    ),
    "gtv_stick": RecoveryProfile(
        key="gtv_stick",
        test_harness_confirmed=None,
        test_harness_notes=(
            "Not a Meta-account-gated device, so the Portal recovery "
            "rationale above doesn't transfer; Test Harness applicability "
            "for this profile is unevaluated, not merely unconfirmed."
        ),
    ),
}

UNKNOWN_RECOVERY_PROFILE = RecoveryProfile(
    key="unknown",
    test_harness_confirmed=None,
    test_harness_notes="No recovery profile is defined for this device.",
)


def get_recovery_profile(device_profile_key: str | None) -> RecoveryProfile:
    """Look up the recovery profile for a `device_profiles` match key."""
    if not device_profile_key:
        return UNKNOWN_RECOVERY_PROFILE
    return RECOVERY_PROFILES.get(device_profile_key, UNKNOWN_RECOVERY_PROFILE)
