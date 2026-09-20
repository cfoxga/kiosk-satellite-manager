"""KSM-TEST-032/033: OEM recovery profile catalog and lookup."""
from __future__ import annotations

from custom_components.kiosk_satellite_manager.oem_recovery import (
    RECOVERY_PROFILES,
    UNKNOWN_RECOVERY_PROFILE,
    get_recovery_profile,
)


def test_get_recovery_profile_returns_unknown_for_unmatched_key():
    assert get_recovery_profile(None) is UNKNOWN_RECOVERY_PROFILE
    assert get_recovery_profile("") is UNKNOWN_RECOVERY_PROFILE
    assert get_recovery_profile("some_future_device") is UNKNOWN_RECOVERY_PROFILE


def test_get_recovery_profile_looks_up_a_known_key():
    profile = get_recovery_profile("portal_mini")
    assert profile.key == "portal_mini"
    assert profile.test_harness_confirmed is True


def test_android_9_portal_generation_stays_unconfirmed():
    """KSM-OPEN-003: no live evidence exists for Gen 1 (Android 9) Portal hardware."""
    profile = get_recovery_profile("portal_gen1")
    assert profile.test_harness_confirmed is None


def test_android_10_plus_portal_generation_is_confirmed():
    profile = get_recovery_profile("portal_gen2")
    assert profile.test_harness_confirmed is True


def test_no_profile_claims_vulnerability_recovery_is_available():
    """KSM-OPEN-005: no vulnerability-based recipe has ever been live-verified."""
    for profile in RECOVERY_PROFILES.values():
        assert profile.vulnerability_recovery_available is False
    assert UNKNOWN_RECOVERY_PROFILE.vulnerability_recovery_available is False


def test_profile_fields_are_fixed_data_never_arbitrary_shell_text():
    """Acceptance: a profile can never carry an arbitrary unverified shell snippet."""
    field_names = set(RECOVERY_PROFILES["portal_mini"].__dataclass_fields__)
    assert "command" not in field_names
    assert "shell" not in field_names
