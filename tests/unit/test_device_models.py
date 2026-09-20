"""Exact device-model identity and fallback classification (KSM-BEHAVE-048).

Replaces `test_device_profiles.py`: `device_profiles.py` combined identity and
provisioning behavior in one record, and issue #20 splits it into
`device_models.py` (identity only) plus `install_recipes.py` (behavior).
"""
from __future__ import annotations

import json
from pathlib import Path

from custom_components.kiosk_satellite_manager.device_models import (
    CLASSIFICATION_GENERIC_ANDROID,
    CLASSIFICATION_GTV_STICK,
    CLASSIFICATION_META_PORTAL,
    CLASSIFICATION_UNKNOWN,
    DEVICE_MODELS,
    DeviceFacts,
    classify_fallback,
    get_device_model,
    match_device_model,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures/profile-portal-go-2026-09-20.json"


def _facts(**kwargs) -> DeviceFacts:
    return DeviceFacts(**kwargs)


def test_portal_go_physical_qualification_matches_exact_observed_facts():
    """KSM-TEST-051 carried forward onto the catalog matcher: a live PortalGo
    qualification must resolve to the exact model, never to a fallback."""
    qualification = json.loads(_FIXTURE.read_text())
    platform = qualification["facts"]["platform"]

    model = match_device_model(DeviceFacts.from_platform(platform))

    assert qualification["evidence"]["source"] == "live_adb_getprop"
    assert model is not None
    assert model.model_key == qualification["expected_model_key"]

    # Negative control: a similarly named Portal SKU must not inherit the
    # PortalGo qualification merely because it shares manufacturer and SDK.
    sibling = match_device_model(
        DeviceFacts.from_platform({**platform, "model": "PortalMini"})
    )
    assert sibling is not None
    assert sibling.model_key != qualification["expected_model_key"]


def test_portal_go_and_portal_mini_are_distinct_exact_models():
    """KSM-TEST-057 (identity half): distinct SKUs, distinct model rows."""
    go = match_device_model(_facts(manufacturer="Facebook", model="PortalGo", sdk=29))
    mini = match_device_model(_facts(manufacturer="Facebook", model="PortalMini", sdk=29))
    assert go is not None and mini is not None
    assert {go.model_key, mini.model_key} == {"portal_go", "portal_mini"}


def test_portal_generations_split_on_the_sdk_boundary():
    """KSM-TEST-059: the same `Portal` model string resolves to a different
    exact model either side of the Android 10 boundary."""
    gen1 = match_device_model(_facts(manufacturer="Facebook", model="Portal", sdk=28))
    gen2 = match_device_model(_facts(manufacturer="Facebook", model="Portal", sdk=29))
    assert gen1 is not None and gen2 is not None
    assert gen1.model_key == "portal_gen1"
    assert gen2.model_key == "portal_gen2"

    plus1 = match_device_model(_facts(manufacturer="Facebook", model="Portal+", sdk=28))
    plus2 = match_device_model(_facts(manufacturer="Facebook", model="PortalPlus", sdk=29))
    assert plus1 is not None and plus2 is not None
    assert plus1.model_key == "portal_plus_gen1"
    assert plus2.model_key == "portal_plus_gen2"


def test_portal_generation_with_missing_sdk_does_not_match_the_wrong_generation():
    """KSM-TEST-059 negative control: an unobserved SDK is missing evidence, not
    a licence to pick a generation."""
    assert match_device_model(_facts(manufacturer="Facebook", model="Portal", sdk=0)) is None
    # Out of range on both sides of every declared generation window.
    assert (
        match_device_model(_facts(manufacturer="Facebook", model="Portal", sdk=21))
        is None
    )


def test_generic_meta_device_is_a_fallback_classification_not_a_model():
    """KSM-TEST-060 (identity half): `meta_portal` is not on the known list."""
    facts = _facts(manufacturer="Facebook")
    assert match_device_model(facts) is None
    assert classify_fallback(facts).key == CLASSIFICATION_META_PORTAL
    assert CLASSIFICATION_META_PORTAL not in {m.model_key for m in DEVICE_MODELS}


def test_broad_tv_stick_match_is_a_fallback_classification_not_a_model():
    """KSM-TEST-060: the old broad `gtv_stick` matcher is not a supported model."""
    for characteristics in ("tv,nosdcard", "tv"):
        facts = _facts(characteristics=characteristics, manufacturer="onn")
        assert match_device_model(facts) is None
        assert classify_fallback(facts).key == CLASSIFICATION_GTV_STICK
    assert CLASSIFICATION_GTV_STICK not in {m.model_key for m in DEVICE_MODELS}


def test_unrelated_device_classifies_generic_and_no_evidence_classifies_unknown():
    assert classify_fallback(_facts(characteristics="automotive", manufacturer="someoem")).key == (
        CLASSIFICATION_GENERIC_ANDROID
    )
    assert classify_fallback(_facts()).key == CLASSIFICATION_UNKNOWN


def test_match_values_are_compared_lower_cased():
    model = match_device_model(_facts(manufacturer="FACEBOOK", model="PORTALGO", sdk=29))
    assert model is not None and model.model_key == "portal_go"


def test_get_device_model_lookup_by_key():
    assert get_device_model("portal_go").model_key == "portal_go"
    assert get_device_model("nonexistent") is None
    assert get_device_model(None) is None


def test_device_model_records_carry_no_provisioning_behavior():
    """KSM-BEHAVE-047 § Source model: identity rows must not own behavior."""
    fields = set(DEVICE_MODELS[0].__dataclass_fields__)
    for forbidden in (
        "permissions",
        "appops",
        "start_url_path",
        "device_name_command",
        "command",
        "shell",
        "recipe",
    ):
        assert forbidden not in fields


def test_portal_tv_is_recorded_as_launcher_incapable_hardware():
    """KSM-TEST-058 (identity half): launcher capability is a hardware fact on
    the model; whether a recipe uses it is recipe behavior."""
    assert get_device_model("portal_tv").home_launcher_capable is False
    assert get_device_model("portal_go").home_launcher_capable is True
