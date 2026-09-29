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
    base = get_recipe("meta_portal_android10")
    return dataclasses.replace(base, **overrides)


def test_every_shipped_recipe_validates():
    """Positive control for the two negative controls below -- if the validator
    rejected everything, the rejection tests would be vacuous."""
    assert INSTALL_RECIPES
    for recipe in INSTALL_RECIPES:
        validate_recipe(recipe)


def test_native_ks_owns_home_settings_and_portal_recipes_have_no_versions():
    """[KSM-TEST-257] Portal recipes have platform names and leave Home to KS."""
    portal_recipes = [r for r in INSTALL_RECIPES if r.recipe_key.startswith("meta_portal")]
    assert {r.recipe_key for r in portal_recipes} == {
        "meta_portal_android9", "meta_portal_android10", "meta_portal_tv",
    }
    for portal in portal_recipes:
        assert not hasattr(portal, "version")
        assert not hasattr(portal, "home_launcher_supported")
        assert "home.enabled" not in dict(portal.parameters)


def test_recipe_keys_are_unique():
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


def test_recipe_validator_rejects_a_new_command_field(monkeypatch):
    """[KSM-TEST-056] A future schema edit cannot expose shell commands."""
    from custom_components.kiosk_satellite_manager.install_recipes import InstallRecipe

    monkeypatch.setattr(
        InstallRecipe,
        "__dataclass_fields__",
        {**InstallRecipe.__dataclass_fields__, "command": InstallRecipe.__dataclass_fields__["recipe_key"]},
    )
    with pytest.raises(RecipeError, match="field 'command'"):
        validate_recipe(_recipe())


def test_recipe_rejects_an_unknown_typed_parameter():
    bad = _recipe(parameters=(("arbitrary_payload", "anything"),))
    with pytest.raises(RecipeError):
        validate_recipe(bad)


def test_recipe_rejects_non_scalar_parameter_and_unknown_policy_fields():
    """[KSM-TEST-056] Data stays typed and closed over declared policy names."""
    with pytest.raises(RecipeError, match="not a typed scalar"):
        validate_recipe(_recipe(parameters=(("browser.ignore_ssl_errors", ("bad",)),)))
    with pytest.raises(RecipeError, match="device_name_source"):
        validate_recipe(_recipe(device_name_source="unknown:source"))
    with pytest.raises(RecipeError, match="permission_policy"):
        validate_recipe(_recipe(permission_policy="unreviewed"))


def test_recipes_are_immutable():
    """KSM-BEHAVE-047: source data, not runtime state."""
    recipe = INSTALL_RECIPES[0]
    assert recipe.__dataclass_params__.frozen is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        recipe.start_url_path = "/nope"  # type: ignore[misc]


def test_portal_recipe_retains_permissions_and_naming():
    """KSM-TEST-063: Portal permission/AppOp/name behavior remains shared."""
    recipe = get_recipe("meta_portal_android10")
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
    assert recipe.device_name_source == "secure:bluetooth_name"
    assert recipe.device_name_command == "settings get secure bluetooth_name"
    assert recipe.sets_device_admin is True


def test_sdk_33_portal_adds_the_modern_media_and_notification_permissions():
    perms = get_recipe("meta_portal_android10").permissions_for_sdk(33)
    assert "android.permission.POST_NOTIFICATIONS" in perms
    assert "android.permission.READ_MEDIA_IMAGES" in perms
    assert "android.permission.BLUETOOTH_SCAN" in perms
    assert "android.permission.WRITE_EXTERNAL_STORAGE" not in perms


def test_android10_portal_models_share_the_same_behavior():
    """[KSM-TEST-257] Android 10 Go, Mini and Gen 2 share behavior."""
    from custom_components.kiosk_satellite_manager.device_catalog import require_recipe
    for model in ("portal_go", "portal_mini", "portal_gen2", "portal_plus_gen2"):
        assert require_recipe(model) is get_recipe("meta_portal_android10")


def test_android9_and_android10_portal_recipes_are_distinct():
    """[KSM-TEST-268] The Android 9 cleanup capability has its own recipe key."""
    from custom_components.kiosk_satellite_manager.device_catalog import require_recipe

    assert require_recipe("portal_gen1").recipe_key == "meta_portal_android9"
    assert require_recipe("portal_plus_gen1").recipe_key == "meta_portal_android9"
    for model in ("portal_go", "portal_mini", "portal_gen2", "portal_plus_gen2"):
        assert require_recipe(model).recipe_key == "meta_portal_android10"
    assert require_recipe("portal_tv").recipe_key == "meta_portal_tv"
    assert get_recipe("meta_portal_android9") is not get_recipe("meta_portal_android10")


def test_android_tv_recipe_exists_but_is_not_portal_shaped():
    """The `android_tv` recipe is declared for future exact onn/Chromecast
    model rows; it must not carry the Portal device-admin/start-URL behavior."""
    recipe = get_recipe("android_tv")
    assert recipe.sets_device_admin is False
    assert recipe.start_url_path == ""
    assert recipe.device_name_source == "global:device_name"
    assert "android.permission.WRITE_SECURE_SETTINGS" not in recipe.permissions_for_sdk(29)


def test_KSM_TEST_270_onn_recipe_carries_android14_special_grants():
    recipe = get_recipe("onn_4k_pro_android14")
    assert recipe is not None
    assert recipe.sets_device_admin is False
    assert recipe.device_name_source == "global:device_name"
    assert {"android.permission.BLUETOOTH_SCAN", "android.permission.BLUETOOTH_CONNECT"} <= set(
        recipe.permissions_for_sdk(34)
    )
    assert {"WRITE_SETTINGS", "MANAGE_EXTERNAL_STORAGE"} <= set(recipe.appops_for_sdk(34))
    assert "converge_consent_services" in recipe.operations
    assert all("converge_consent_services" in r.operations for r in INSTALL_RECIPES)


def test_device_name_normalization_strips_the_model_suffix_android_appends():
    recipe = get_recipe("meta_portal_android10")
    assert recipe.normalize_device_name("Kitchen PortalGo") == "Kitchen"
    assert recipe.normalize_device_name("Kitchen") == "Kitchen"
    # A label that is only the suffix is left alone rather than emptied.
    assert recipe.normalize_device_name("PortalGo") == "PortalGo"


def test_get_recipe_returns_none_for_an_unknown_key():
    assert get_recipe("no_such_recipe") is None
    assert get_recipe(None) is None


def test_recipe_validation_scans_every_string_field_not_an_enrolled_subset():
    """KSM-TEST-056: the metacharacter scan is an exemption list (`name`,
    `postconditions`), not an allowlist of enrolled fields. A field added to
    `InstallRecipe` later must be scanned by default rather than escaping the
    check because nobody remembered to enrol it.

    Proven field-by-field: every non-prose string field rejects a smuggled
    command, and the two prose fields accept ordinary punctuation.
    """
    recipe = get_recipe("meta_portal_android10")
    scanned = [
        f.name
        for f in dataclasses.fields(recipe)
        if f.name not in ("name", "postconditions")
        and isinstance(getattr(recipe, f.name), str)
    ]
    assert len(scanned) >= 7, scanned
    for field_name in scanned:
        smuggled = dataclasses.replace(recipe, **{field_name: "x; rm -rf /data"})
        # `recipe_key` has a stricter format rule that rejects first;
        # every other field is caught by the metacharacter scan itself.
        expected = (
            "not a stable lower_snake key|must look like"
            if field_name == "recipe_key"
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
    recipe = get_recipe("meta_portal_android10")
    assert OP_SET_DEVICE_ADMIN in recipe.operations and recipe.sets_device_admin

    undeclared = dataclasses.replace(
        recipe, operations=tuple(o for o in recipe.operations if o != OP_SET_DEVICE_ADMIN)
    )
    with pytest.raises(RecipeError, match="does not declare operation"):
        validate_recipe(undeclared)

    tv = get_recipe("android_tv")
    assert OP_SET_DEVICE_ADMIN not in tv.operations and tv.sets_device_admin is False
    overclaimed = dataclasses.replace(tv, operations=tv.operations + (OP_SET_DEVICE_ADMIN,))
    with pytest.raises(RecipeError, match="sets_device_admin is False"):
        validate_recipe(overclaimed)
