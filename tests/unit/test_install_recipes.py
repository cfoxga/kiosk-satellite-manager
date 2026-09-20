"""Versioned, typed install recipes (KSM-BEHAVE-049, KSM-TEST-056)."""
from __future__ import annotations

import dataclasses

import pytest

from custom_components.kiosk_satellite_manager.install_recipes import (
    ALLOWED_OPERATIONS,
    INSTALL_RECIPES,
    OP_SET_DEVICE_ADMIN,
    RecipeError,
    get_recipe,
    validate_recipe,
)


def _recipe(**overrides):
    base = get_recipe("meta_portal_standard", "v2")
    return dataclasses.replace(base, **overrides)


def test_every_shipped_recipe_validates():
    """Positive control for the two negative controls below -- if the validator
    rejected everything, the rejection tests would be vacuous."""
    assert INSTALL_RECIPES
    for recipe in INSTALL_RECIPES:
        validate_recipe(recipe)


def test_recipe_keys_and_versions_are_unique():
    identities = [r.identity for r in INSTALL_RECIPES]
    assert len(identities) == len(set(identities))


def test_recipe_rejects_an_undeclared_operation_identifier():
    """KSM-TEST-056: only audited operation identifiers are executable."""
    bad = _recipe(operations=("pm_install", "curl_a_script_and_run_it"))
    with pytest.raises(RecipeError) as err:
        validate_recipe(bad)
    assert "curl_a_script_and_run_it" in str(err.value)
    assert "curl_a_script_and_run_it" not in ALLOWED_OPERATIONS


def test_recipe_rejects_arbitrary_shell_text_smuggled_into_a_typed_field():
    """KSM-TEST-056: a recipe has no command field at all, and no typed string
    field may carry shell metacharacters that would turn one into a command."""
    fields = set(INSTALL_RECIPES[0].__dataclass_fields__)
    for forbidden in ("command", "commands", "shell", "script", "cmd", "exec"):
        assert forbidden not in fields

    smuggled = _recipe(device_name_source="global:device_name; rm -rf /data")
    with pytest.raises(RecipeError) as err:
        validate_recipe(smuggled)
    assert "device_name_source" in str(err.value)


def test_recipe_rejects_an_unknown_typed_parameter():
    bad = _recipe(parameters=(("arbitrary_payload", "anything"),))
    with pytest.raises(RecipeError):
        validate_recipe(bad)


def test_recipes_are_immutable():
    """KSM-BEHAVE-047: source data, not runtime state."""
    recipe = INSTALL_RECIPES[0]
    assert recipe.__dataclass_params__.frozen is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        recipe.start_url_path = "/nope"  # type: ignore[misc]


def test_portal_standard_recipe_reproduces_the_pre_catalog_portal_behavior():
    """KSM-TEST-063 (unit half): the migrated recipe must resolve the exact
    permission/appop/launcher/name behavior `DeviceProfile` shipped."""
    recipe = get_recipe("meta_portal_standard", "v2")
    perms = recipe.permissions_for_sdk(29)
    assert perms == [
        "android.permission.RECORD_AUDIO",
        "android.permission.CAMERA",
        "android.permission.ACCESS_COARSE_LOCATION",
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.READ_LOGS",
        "android.permission.READ_EXTERNAL_STORAGE",
        "android.permission.WRITE_EXTERNAL_STORAGE",
        "android.permission.WRITE_SECURE_SETTINGS",
    ]
    assert recipe.appops_for_sdk(29) == [
        "SYSTEM_ALERT_WINDOW",
        "WRITE_SETTINGS",
        "GET_USAGE_STATS",
    ]
    assert "android.permission.MANAGE_EXTERNAL_STORAGE" not in perms
    assert recipe.appops_for_sdk(30)[-1] == "MANAGE_EXTERNAL_STORAGE"
    assert recipe.start_url_path == "/portal"
    assert recipe.home_launcher_supported is True
    assert recipe.device_name_source == "secure:bluetooth_name"
    assert recipe.device_name_command == "settings get secure bluetooth_name"
    assert recipe.sets_device_admin is True


def test_sdk_33_portal_adds_the_modern_media_and_notification_permissions():
    perms = get_recipe("meta_portal_standard", "v2").permissions_for_sdk(33)
    assert "android.permission.POST_NOTIFICATIONS" in perms
    assert "android.permission.READ_MEDIA_IMAGES" in perms
    assert "android.permission.BLUETOOTH_SCAN" in perms
    assert "android.permission.WRITE_EXTERNAL_STORAGE" not in perms


def test_portal_tv_recipe_is_not_launcher_capable():
    """KSM-TEST-058: Portal TV's recipe differs precisely in launcher behavior."""
    tv = get_recipe("meta_portal_tv", "v2")
    assert tv.home_launcher_supported is False
    assert tv.start_url_path == "/portal"


def test_android_tv_recipe_exists_but_is_not_portal_shaped():
    """The `android_tv:v1` recipe is declared for future exact onn/Chromecast
    model rows; it must not carry the Portal device-admin/start-URL behavior."""
    recipe = get_recipe("android_tv", "v1")
    assert recipe.sets_device_admin is False
    assert recipe.start_url_path == ""
    assert recipe.device_name_source == "global:device_name"
    assert "android.permission.WRITE_SECURE_SETTINGS" not in recipe.permissions_for_sdk(29)


def test_device_name_normalization_strips_the_model_suffix_android_appends():
    recipe = get_recipe("meta_portal_standard", "v2")
    assert recipe.normalize_device_name("Kitchen PortalGo") == "Kitchen"
    assert recipe.normalize_device_name("Kitchen") == "Kitchen"
    # A label that is only the suffix is left alone rather than emptied.
    assert recipe.normalize_device_name("PortalGo") == "PortalGo"


def test_get_recipe_returns_none_for_an_unknown_key_or_version():
    assert get_recipe("meta_portal_standard", "v99") is None
    assert get_recipe("no_such_recipe", "v1") is None


def test_recipe_validation_scans_every_string_field_not_an_enrolled_subset():
    """KSM-TEST-056: the metacharacter scan is an exemption list (`name`,
    `postconditions`), not an allowlist of enrolled fields. A field added to
    `InstallRecipe` later must be scanned by default rather than escaping the
    check because nobody remembered to enrol it.

    Proven field-by-field: every non-prose string field rejects a smuggled
    command, and the two prose fields accept ordinary punctuation.
    """
    recipe = get_recipe("meta_portal_standard", "v2")
    scanned = [
        f.name
        for f in dataclasses.fields(recipe)
        if f.name not in ("name", "postconditions")
        and isinstance(getattr(recipe, f.name), str)
    ]
    assert len(scanned) >= 8, scanned
    for field_name in scanned:
        smuggled = dataclasses.replace(recipe, **{field_name: "x; rm -rf /data"})
        # `recipe_key`/`version` have stricter format rules that reject first;
        # every other field is caught by the metacharacter scan itself.
        expected = (
            "not a stable lower_snake key|must look like"
            if field_name in ("recipe_key", "version")
            else "shell metacharacter"
        )
        with pytest.raises(RecipeError, match=expected):
            validate_recipe(smuggled)

    # Positive control: prose really is exempt, so the test above is about the
    # field set and not about the string happening to contain a semicolon.
    validate_recipe(dataclasses.replace(recipe, name="Portal (standard); v1"))


def test_recipe_operations_cannot_drift_from_what_install_actually_branches_on():
    """KSM-TEST-056: the operation list is the audited surface a reviewer
    reads; the booleans are what `install.py` executes. If the two can
    disagree the list is decoration, so `validate_recipe` rejects both
    directions of drift."""
    recipe = get_recipe("meta_portal_standard", "v2")
    assert OP_SET_DEVICE_ADMIN in recipe.operations and recipe.sets_device_admin

    undeclared = dataclasses.replace(
        recipe, operations=tuple(o for o in recipe.operations if o != OP_SET_DEVICE_ADMIN)
    )
    with pytest.raises(RecipeError, match="does not declare operation"):
        validate_recipe(undeclared)

    tv = get_recipe("android_tv", "v1")
    assert OP_SET_DEVICE_ADMIN not in tv.operations and tv.sets_device_admin is False
    overclaimed = dataclasses.replace(tv, operations=tv.operations + (OP_SET_DEVICE_ADMIN,))
    with pytest.raises(RecipeError, match="sets_device_admin is False"):
        validate_recipe(overclaimed)
