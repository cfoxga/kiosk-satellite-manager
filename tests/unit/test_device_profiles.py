"""Unit tests for device-type profile matching (KSM-BEHAVE-001)."""
from custom_components.kiosk_satellite_manager.device_profiles import (
    UNKNOWN_PROFILE,
    match_profile,
)


def test_onn_gtv_stick_matches_by_characteristics_and_manufacturer():
    # live-verified getprop values from a real onn 4K Pro (docs/SPEC/provisioning.md Finding 4)
    profile = match_profile("tv,nosdcard", "onn")
    assert profile.key == "gtv_stick"


def test_onn_4k_matches_shorter_characteristics_value():
    profile = match_profile("tv", "onn")
    assert profile.key == "gtv_stick"


def test_meta_portal_matches_by_manufacturer_only():
    profile = match_profile("", "facebook")
    assert profile.key == "meta_portal"


def test_unrecognized_device_falls_back_to_unknown():
    profile = match_profile("automotive", "someoem")
    assert profile is UNKNOWN_PROFILE


def test_match_is_case_insensitive():
    profile = match_profile("TV,NOSDCARD", "ONN")
    assert profile.key == "gtv_stick"
