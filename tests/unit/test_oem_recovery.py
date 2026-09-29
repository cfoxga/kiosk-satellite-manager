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


def test_android_10_plus_portal_generation_stays_unconfirmed():
    """KSM-OPEN-003: live evidence is on portal_mini hardware, not the distinct
    portal_gen2 ("portal") model string -- platform-family resemblance is not
    a substitute for a direct confirmation on this profile."""
    profile = get_recovery_profile("portal_gen2")
    assert profile.test_harness_confirmed is None


def test_portal_go_test_harness_recovery_is_exact_model_confirmed():
    """KSM-TEST-122: PortalGo's confirmation comes from its own live reset."""
    profile = get_recovery_profile("portal_go")

    assert profile.test_harness_confirmed is True
    assert profile.postconditions
    assert "PortalGo" in profile.test_harness_notes


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


def test_every_recovery_profile_is_keyed_on_an_exact_device_model():
    """KSM-TEST-066 (issue #20): recovery qualification is per exact model.
    Before the catalog this dict also held "meta_portal" and "gtv_stick",
    which are fallback *classifications* -- a classification owning
    destructive-recovery evidence is exactly the inheritance the issue
    forbids. The catalog's own recovery_profile_keys must agree, so a future
    row added on only one side fails here."""
    from custom_components.kiosk_satellite_manager.device_catalog import CATALOG
    from custom_components.kiosk_satellite_manager.device_models import (
        FALLBACK_CLASSIFICATIONS,
        get_device_model,
    )

    assert set(RECOVERY_PROFILES) == set(CATALOG.recovery_profile_keys)
    for key in RECOVERY_PROFILES:
        assert get_device_model(key) is not None, key
    # Positive control: the classification keys are real strings that simply
    # must not appear here -- so the assertion above could have failed.
    classification_keys = {c.key for c in FALLBACK_CLASSIFICATIONS}
    assert classification_keys
    assert classification_keys.isdisjoint(RECOVERY_PROFILES)


def test_shared_install_recipe_does_not_share_recovery_evidence():
    """KSM-TEST-066: recipe assignment never owns exact-model recovery proof."""
    from custom_components.kiosk_satellite_manager.device_catalog import require_recipe

    assert require_recipe("portal_go") is require_recipe("portal_mini")
    assert get_recovery_profile("portal_mini").test_harness_confirmed is True
    assert get_recovery_profile("portal_go").test_harness_confirmed is True
    assert get_recovery_profile("portal_gen2").test_harness_confirmed is None
