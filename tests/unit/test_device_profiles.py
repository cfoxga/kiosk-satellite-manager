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


def test_portal_go_matches_by_model():
    profile = match_profile("nosdcard", "Facebook", model="PortalGo", sdk=29)
    assert profile.key == "portal_go"
    assert profile.name == "Meta Portal Go"
    assert profile.start_url_path == "/portal"
    assert profile.is_portal is True


def test_portal_mini_matches_by_model():
    profile = match_profile("nosdcard", "Facebook", model="PortalMini", sdk=29)
    assert profile.key == "portal_mini"
    assert profile.name == "Meta Portal Mini"
    assert profile.start_url_path == "/portal"


def test_portal_gen2_matches_model_with_android_10():
    profile = match_profile("nosdcard", "Facebook", model="Portal", sdk=29)
    assert profile.key == "portal_gen2"
    assert profile.name == "Meta Portal (Gen 2)"
    assert profile.start_url_path == "/portal"


def test_portal_gen1_matches_model_with_android_9():
    profile = match_profile("nosdcard", "Facebook", model="Portal", sdk=28)
    assert profile.key == "portal_gen1"
    assert profile.name == "Meta Portal (Gen 1)"
    assert profile.start_url_path == "/portal"


def test_portal_plus_matches_by_model_and_sdk():
    p_gen2 = match_profile("nosdcard", "Facebook", model="Portal+", sdk=29)
    assert p_gen2.key == "portal_plus_gen2"
    assert p_gen2.name == "Meta Portal+ (Gen 2)"

    p_gen1 = match_profile("nosdcard", "Facebook", model="Portal+", sdk=28)
    assert p_gen1.key == "portal_plus_gen1"
    assert p_gen1.name == "Meta Portal+ (Gen 1)"


def test_portal_tv_matches_by_model():
    profile = match_profile("tv", "Facebook", model="PortalTV", sdk=29)
    assert profile.key == "portal_tv"
    assert profile.name == "Meta Portal TV"


def test_get_profile_lookup_by_key():
    from custom_components.kiosk_satellite_manager.device_profiles import get_profile
    profile = get_profile("portal_go")
    assert profile.key == "portal_go"
    unknown = get_profile("nonexistent")
    assert unknown.key == "unknown"


def test_normalize_device_name_strips_model_suffixes():
    profile = match_profile("nosdcard", "Facebook", model="PortalGo", sdk=29)
    assert profile.normalize_device_name("Test Portal Portal") == "Test Portal"
    assert profile.normalize_device_name("Kitchen PortalMini") == "Kitchen"
    assert profile.normalize_device_name("Office PortalGo") == "Office"
    # Single word "Portal" is preserved and not stripped to empty string
    assert profile.normalize_device_name("Portal") == "Portal"


def test_permissions_gated_by_sdk():
    profile = match_profile("nosdcard", "Facebook", model="PortalGo", sdk=29)
    perms_29 = profile.permissions_for_sdk(29)
    assert "android.permission.WRITE_EXTERNAL_STORAGE" in perms_29
    assert "android.permission.BLUETOOTH_SCAN" not in perms_29
    assert "android.permission.POST_NOTIFICATIONS" not in perms_29

    perms_31 = profile.permissions_for_sdk(31)
    assert "android.permission.BLUETOOTH_SCAN" in perms_31
    assert "android.permission.WRITE_EXTERNAL_STORAGE" not in perms_31

    perms_33 = profile.permissions_for_sdk(33)
    assert "android.permission.POST_NOTIFICATIONS" in perms_33
    assert "android.permission.READ_MEDIA_IMAGES" in perms_33


def test_unrecognized_device_falls_back_to_unknown():
    profile = match_profile("automotive", "someoem")
    assert profile is UNKNOWN_PROFILE


def test_match_is_case_insensitive():
    profile = match_profile("TV,NOSDCARD", "ONN")
    assert profile.key == "gtv_stick"

