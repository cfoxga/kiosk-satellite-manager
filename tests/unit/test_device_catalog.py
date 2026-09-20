"""Source-maintained device catalog: assignments, qualification evidence and
derived support state (KSM-BEHAVE-047/049/050, KSM-TEST-055/057/058/061/062)."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from custom_components.kiosk_satellite_manager import device_catalog
from custom_components.kiosk_satellite_manager.device_catalog import (
    ASSIGNMENT_APPROVED,
    ASSIGNMENT_PROPOSED,
    CATALOG,
    SUPPORT_BLOCKED,
    SUPPORT_PARTIALLY_QUALIFIED,
    SUPPORT_RECIPE_ASSIGNED,
    SUPPORT_RECOGNIZED,
    SUPPORT_REVALIDATION_REQUIRED,
    SUPPORT_SUPPORTED,
    SUPPORT_UNKNOWN,
    CatalogError,
    QualificationRecord,
    RecipeAssignment,
    required_scenarios,
    resolve_catalog_entry,
    validate_catalog,
)
from custom_components.kiosk_satellite_manager.device_models import DeviceFacts
from custom_components.kiosk_satellite_manager.install_recipes import get_recipe

_COMPONENT = Path(device_catalog.__file__).parent

_PORTAL_GO = DeviceFacts(manufacturer="Facebook", model="PortalGo", sdk=29)
_PORTAL_MINI = DeviceFacts(manufacturer="Facebook", model="PortalMini", sdk=29)
_PORTAL_TV = DeviceFacts(manufacturer="Facebook", model="PortalTV", characteristics="tv", sdk=29)


def _qualifications(model_key: str, recipe_version: str = "v2", *, result: str = "pass"):
    recipe = get_recipe("meta_portal_standard", recipe_version) or get_recipe(
        "meta_portal_standard", "v2"
    )
    return tuple(
        QualificationRecord(
            model_key=model_key,
            recipe_key="meta_portal_standard",
            recipe_version=recipe_version,
            scenario=scenario,
            result=result,
            min_sdk=29,
            max_sdk=29,
            verified_on="2026-09-20",
            evidence="synthetic record for this unit test",
            positive_control="the same scenario observed passing on the device",
            negative_control="a deliberately inverted expectation failed first",
        )
        for scenario in sorted(required_scenarios(recipe))
    )


def _catalog(**overrides):
    return dataclasses.replace(CATALOG, **overrides)


# --- KSM-TEST-055: schema integrity -----------------------------------------


def test_shipped_catalog_validates():
    """Positive control: every rejection test below would be vacuous if the
    validator rejected the real catalog too."""
    validate_catalog(CATALOG)


def test_catalog_rejects_duplicate_model_keys():
    models = CATALOG.models + (CATALOG.models[0],)
    with pytest.raises(CatalogError, match="duplicate device model"):
        validate_catalog(_catalog(models=models))


def test_catalog_rejects_duplicate_recipe_versions():
    recipes = CATALOG.recipes + (CATALOG.recipes[0],)
    with pytest.raises(CatalogError, match="duplicate install recipe"):
        validate_catalog(_catalog(recipes=recipes))


def test_catalog_rejects_an_assignment_referencing_an_unknown_model():
    assignments = CATALOG.assignments + (
        RecipeAssignment(
            model_key="no_such_model",
            recipe_key="meta_portal_standard",
            recipe_version="v2",
            state=ASSIGNMENT_APPROVED,
            effective_date="2026-09-20",
            rationale="deliberately broken reference",
        ),
    )
    with pytest.raises(CatalogError, match="unknown device model"):
        validate_catalog(_catalog(assignments=assignments))


def test_catalog_rejects_an_assignment_referencing_an_unknown_recipe_version():
    assignments = CATALOG.assignments + (
        RecipeAssignment(
            model_key="portal_go",
            recipe_key="meta_portal_standard",
            recipe_version="v97",
            state=ASSIGNMENT_PROPOSED,
            effective_date="2026-09-20",
            rationale="deliberately broken reference",
        ),
    )
    with pytest.raises(CatalogError, match="unknown install recipe"):
        validate_catalog(_catalog(assignments=assignments))


def test_catalog_rejects_two_approved_assignments_for_one_model():
    assignments = CATALOG.assignments + (
        RecipeAssignment(
            model_key="portal_go",
            recipe_key="meta_portal_tv",
            recipe_version="v2",
            state=ASSIGNMENT_APPROVED,
            effective_date="2026-09-20",
            rationale="deliberately ambiguous",
        ),
    )
    with pytest.raises(CatalogError, match="more than one approved"):
        validate_catalog(_catalog(assignments=assignments))


def test_catalog_rejects_qualification_for_an_unassigned_recipe():
    quals = CATALOG.qualifications + _qualifications("portal_go", "v97")
    with pytest.raises(CatalogError, match="unknown install recipe"):
        validate_catalog(_catalog(qualifications=quals))


def test_catalog_rejects_a_recovery_profile_keyed_on_a_fallback_classification():
    """KSM-BEHAVE-052: recovery evidence is per exact model, so a fallback
    classification can never own a recovery row."""
    with pytest.raises(CatalogError, match="unknown device model"):
        validate_catalog(_catalog(recovery_profile_keys=("meta_portal",)))


def test_catalog_records_are_frozen():
    """KSM-TEST-055: mutable/runtime-backed catalog definitions are rejected."""
    for table in (CATALOG.models, CATALOG.recipes, CATALOG.assignments, CATALOG.qualifications):
        for record in table:
            assert record.__dataclass_params__.frozen is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        CATALOG.models[0].name = "mutated"  # type: ignore[misc]


def test_catalog_modules_use_no_sql_or_home_assistant_storage():
    """KSM-TEST-055: catalog persistence through SQL or HA `.storage` is
    prohibited -- the catalog is source, not runtime state."""
    sources = "\n".join(
        (_COMPONENT / name).read_text()
        for name in ("device_models.py", "install_recipes.py", "device_catalog.py")
    )
    for banned in ("sqlite3", "sqlalchemy", "helpers.storage", "Store(", ".storage"):
        assert banned not in sources
    # Positive control: the string check can fail -- one of these really is there.
    assert "dataclass" in sources


# --- KSM-TEST-057/058: resolution -------------------------------------------


def test_portal_go_and_mini_are_distinct_models_sharing_one_recipe_version():
    """KSM-TEST-057."""
    go = resolve_catalog_entry(_PORTAL_GO)
    mini = resolve_catalog_entry(_PORTAL_MINI)

    assert go.model_key == "portal_go"
    assert mini.model_key == "portal_mini"
    assert go.recipe_identity == mini.recipe_identity == "meta_portal_standard:v2"
    assert go.recipe is mini.recipe
    assert go.executable is True


def test_sharing_a_recipe_never_shares_qualification_evidence():
    """KSM-TEST-057 negative control: Mini's evidence stays Mini's."""
    catalog = _catalog(qualifications=CATALOG.qualifications + _qualifications("portal_mini"))
    go = resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29)
    mini = resolve_catalog_entry(_PORTAL_MINI, catalog=catalog, sdk=29)

    assert mini.support_state == SUPPORT_SUPPORTED
    assert go.support_state == SUPPORT_RECIPE_ASSIGNED


def test_portal_tv_resolves_the_launcher_incapable_recipe():
    """KSM-TEST-058."""
    tv = resolve_catalog_entry(_PORTAL_TV)
    assert tv.model_key == "portal_tv"
    assert tv.recipe_identity == "meta_portal_tv:v2"
    assert tv.recipe.home_launcher_supported is False


def test_inverted_portal_tv_assignment_fails_catalog_validation():
    """KSM-TEST-058 negative control: assigning the launcher-capable standard
    recipe to launcher-incapable hardware is a data error, not a runtime
    surprise."""
    assignments = tuple(
        a
        for a in CATALOG.assignments
        if not (a.model_key == "portal_tv" and a.state == ASSIGNMENT_APPROVED)
    ) + (
        RecipeAssignment(
            model_key="portal_tv",
            recipe_key="meta_portal_standard",
            recipe_version="v2",
            state=ASSIGNMENT_APPROVED,
            effective_date="2026-09-20",
            rationale="deliberately inverted",
        ),
    )
    with pytest.raises(CatalogError, match="home launcher"):
        validate_catalog(_catalog(assignments=assignments))


# --- KSM-TEST-060: fail closed ----------------------------------------------


def test_fallback_classification_reports_itself_but_resolves_no_recipe():
    """KSM-TEST-060."""
    generic_portal = resolve_catalog_entry(DeviceFacts(manufacturer="Facebook"))
    assert generic_portal.model_key is None
    assert generic_portal.classification == "meta_portal"
    assert generic_portal.recipe is None
    assert generic_portal.executable is False
    assert generic_portal.support_state == SUPPORT_UNKNOWN
    assert "no exact device model" in generic_portal.reason

    stick = resolve_catalog_entry(DeviceFacts(manufacturer="onn", characteristics="tv"))
    assert stick.classification == "gtv_stick"
    assert stick.recipe is None

    nothing = resolve_catalog_entry(DeviceFacts())
    assert nothing.classification == "unknown"
    assert nothing.recipe is None


def test_a_recognized_model_without_an_approved_assignment_fails_closed():
    assignments = tuple(
        dataclasses.replace(a, state=ASSIGNMENT_PROPOSED) if a.model_key == "portal_go" else a
        for a in CATALOG.assignments
    )
    entry = resolve_catalog_entry(_PORTAL_GO, catalog=_catalog(assignments=assignments))
    assert entry.model_key == "portal_go"
    assert entry.recipe is None
    assert entry.executable is False
    assert entry.support_state == SUPPORT_RECOGNIZED


# --- KSM-TEST-061/062: derived support state --------------------------------


def test_full_matrix_yields_supported_only_for_the_qualified_model():
    """KSM-TEST-061."""
    catalog = _catalog(qualifications=CATALOG.qualifications + _qualifications("portal_go"))
    assert resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29).support_state == (
        SUPPORT_SUPPORTED
    )
    assert resolve_catalog_entry(_PORTAL_MINI, catalog=catalog, sdk=29).support_state == (
        SUPPORT_RECIPE_ASSIGNED
    )


def test_a_partial_matrix_is_partially_qualified_not_supported():
    """KSM-TEST-061: evidence for some scenarios is not evidence for all."""
    partial = _qualifications("portal_go")[:2]
    catalog = _catalog(qualifications=CATALOG.qualifications + partial)
    assert resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29).support_state == (
        SUPPORT_PARTIALLY_QUALIFIED
    )


def test_a_failed_scenario_blocks_the_model():
    quals = _qualifications("portal_go")[:-1] + (
        dataclasses.replace(_qualifications("portal_go")[-1], result="fail"),
    )
    catalog = _catalog(qualifications=CATALOG.qualifications + quals)
    entry = resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29)
    assert entry.support_state == SUPPORT_BLOCKED


def test_evidence_for_an_earlier_recipe_version_goes_stale_on_a_new_version():
    """KSM-TEST-062: a behavior-changing v2 makes prior qualification stale
    rather than letting it carry over on key-only matching."""
    v2 = dataclasses.replace(
        get_recipe("meta_portal_standard", "v2"),
        version="v2",
        start_url_path="/portal-v2",
    )
    assignments = tuple(
        dataclasses.replace(a, recipe_version="v2") if a.model_key == "portal_go" else a
        for a in CATALOG.assignments
    )
    catalog = _catalog(
        recipes=CATALOG.recipes + (v2,),
        assignments=assignments,
        qualifications=CATALOG.qualifications + _qualifications("portal_go", "v1"),
    )
    entry = resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29)
    assert entry.recipe_identity == "meta_portal_standard:v2"
    assert entry.support_state == SUPPORT_REVALIDATION_REQUIRED
    assert "v1" in entry.reason


def test_evidence_outside_the_observed_build_scope_requires_revalidation():
    """KSM-TEST-062: an SDK outside the evidence's build scope is out of scope,
    not silently covered."""
    catalog = _catalog(qualifications=CATALOG.qualifications + _qualifications("portal_go"))
    in_scope = resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29)
    out_of_scope = resolve_catalog_entry(
        dataclasses.replace(_PORTAL_GO, sdk=33), catalog=catalog, sdk=33
    )
    assert in_scope.support_state == SUPPORT_SUPPORTED
    assert out_of_scope.support_state == SUPPORT_REVALIDATION_REQUIRED
    assert "build scope" in out_of_scope.reason


def test_required_scenarios_drop_launcher_selection_for_a_launcher_incapable_recipe():
    standard = required_scenarios(get_recipe("meta_portal_standard", "v2"))
    tv = required_scenarios(get_recipe("meta_portal_tv", "v2"))
    assert "launcher_selection" in standard
    assert "launcher_selection" not in tv
    assert {"clean_install", "existing_reuse", "update", "reinstall", "uninstall"} <= tv


def test_shipped_catalog_claims_no_unearned_support():
    """No hand-maintained `supported=True`: every shipped model's state is
    derived, and none of them claims `supported` without a matrix on file."""
    for model in CATALOG.models:
        entry = resolve_catalog_entry(
            DeviceFacts(
                manufacturer=model.manufacturer[0] if model.manufacturer else "",
                model=model.models[0] if model.models else "",
                sdk=model.min_sdk or 29,
            )
        )
        if entry.support_state == SUPPORT_SUPPORTED:
            assert any(q.model_key == model.model_key for q in CATALOG.qualifications)


def test_catalog_rejects_a_passing_qualification_with_no_evidence():
    """KSM-TEST-055: `supported` is derived from these records, so a record
    that asserts a pass without saying what was observed is the hand-maintained
    boolean the catalog exists to remove, wearing a dataclass."""
    blank = tuple(
        dataclasses.replace(record, evidence="   ")
        for record in _qualifications("portal_go")
    )
    with pytest.raises(CatalogError, match="no evidence recorded"):
        validate_catalog(_catalog(qualifications=blank))


def test_catalog_rejects_a_passing_qualification_with_no_build_scope():
    """KSM-TEST-062: `covers()` short-circuits to True for an empty scope, so
    an unscoped pass would qualify a device whose SDK was never read at all.
    Positive control: the same records with their SDK range restored validate,
    proving the rejection is about the scope and not the rest of the record."""
    unscoped = tuple(
        dataclasses.replace(record, min_sdk=None, max_sdk=None, fingerprint_prefixes=())
        for record in _qualifications("portal_go")
    )
    with pytest.raises(CatalogError, match="no build scope"):
        validate_catalog(_catalog(qualifications=unscoped))

    validate_catalog(_catalog(qualifications=_qualifications("portal_go")))


def test_catalog_rejects_a_launcher_recipe_on_launcher_incapable_hardware_while_proposed():
    """KSM-TEST-058: the launcher check must not wait for `approved`. A
    proposed assignment is a source defect now; discovering it at the moment
    somebody flips the state to approved is the worst possible time."""
    proposed = CATALOG.assignments + (
        RecipeAssignment(
            model_key="portal_tv",
            recipe_key="meta_portal_standard",
            recipe_version="v2",
            state=ASSIGNMENT_PROPOSED,
            effective_date="2026-09-20",
            rationale="deliberately wrong: Portal TV cannot host a home launcher",
        ),
    )
    with pytest.raises(CatalogError, match="home launcher incapable"):
        validate_catalog(_catalog(assignments=proposed))
