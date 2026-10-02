"""Source-maintained device catalog: assignments, qualification evidence and
derived support state (KSM-BEHAVE-047/049/050, KSM-TEST-055/057/058/061/062)."""
from __future__ import annotations

import dataclasses
import json
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
    derive_support_state,
    require_recipe,
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


def test_KSM_TEST_270_onn_4k_pro_android14_resolves_only_exact_live_identity():
    facts = DeviceFacts(
        manufacturer="onn", model="onn 4K Pro Streaming Device",
        device="jarvis2", characteristics="tv,nosdcard", sdk=34,
    )
    resolution = resolve_catalog_entry(facts)
    assert resolution.model_key == "onn_4k_pro_android14"
    assert resolution.executable
    assert resolution.recipe.recipe_key == "onn_4k_pro_android14"
    health_facts = DeviceFacts.from_health({
        "brand": "onn", "model": "onn onn 4K Pro Streaming Device", "sdkInt": 34,
    })
    assert resolve_catalog_entry(health_facts).recipe.recipe_key == "onn_4k_pro_android14"
    for changed in (
        dataclasses.replace(facts, model="onn 4K Streaming Device"),
        dataclasses.replace(facts, model=""),
        dataclasses.replace(facts, manufacturer="google"),
        dataclasses.replace(facts, sdk=33),
        dataclasses.replace(facts, sdk=0),
    ):
        assert not resolve_catalog_entry(changed).executable


def _qualifications(model_key: str, recipe_key: str = "meta_portal_android10_local_dns", *, result: str = "pass"):
    recipe = get_recipe("meta_portal_android10_local_dns")
    return tuple(
        QualificationRecord(
            model_key=model_key,
            recipe_key=recipe_key,
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


def test_catalog_rejects_duplicate_recipe_keys():
    recipes = CATALOG.recipes + (CATALOG.recipes[0],)
    with pytest.raises(CatalogError, match="duplicate install recipe"):
        validate_catalog(_catalog(recipes=recipes))


def test_catalog_rejects_an_assignment_referencing_an_unknown_model():
    assignments = CATALOG.assignments + (
        RecipeAssignment(
            model_key="no_such_model",
            recipe_key="meta_portal_android10_local_dns",
            state=ASSIGNMENT_APPROVED,
            effective_date="2026-09-20",
            rationale="deliberately broken reference",
        ),
    )
    with pytest.raises(CatalogError, match="unknown device model"):
        validate_catalog(_catalog(assignments=assignments))


def test_catalog_rejects_an_assignment_referencing_an_unknown_recipe_key():
    assignments = CATALOG.assignments + (
        RecipeAssignment(
            model_key="portal_go",
            recipe_key="unknown_recipe",
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
            recipe_key="meta_portal_android10_local_dns",
            state=ASSIGNMENT_APPROVED,
            effective_date="2026-09-20",
            rationale="deliberately ambiguous",
        ),
    )
    with pytest.raises(CatalogError, match="more than one approved"):
        validate_catalog(_catalog(assignments=assignments))


def test_catalog_rejects_qualification_for_an_unassigned_recipe():
    quals = _qualifications("portal_go", "unknown_recipe")
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


def test_catalog_rejects_mutable_rows_and_unknown_assignment_state():
    """[KSM-TEST-055] Each source table rejects runtime-mutable data."""
    MutableModel = dataclasses.make_dataclass("MutableModel", [("model_key", str)])
    with pytest.raises(CatalogError, match="device model .* mutable"):
        validate_catalog(_catalog(models=(MutableModel("mutable"),)))

    MutableRecipe = dataclasses.make_dataclass("MutableRecipe", [("identity", str)])
    with pytest.raises(CatalogError, match="install recipe .* mutable"):
        validate_catalog(_catalog(recipes=(MutableRecipe("mutable:v1"),)))

    MutableAssignment = dataclasses.make_dataclass("MutableAssignment", [("model_key", str)])
    with pytest.raises(CatalogError, match="assignment .* mutable"):
        validate_catalog(_catalog(assignments=(MutableAssignment("portal_go"),)))

    invalid_state = dataclasses.replace(CATALOG.assignments[0], state="unreviewed")
    with pytest.raises(CatalogError, match="undeclared state"):
        validate_catalog(_catalog(assignments=(invalid_state,)))


def test_qualification_scope_and_validation_reject_untrusted_claims():
    """[KSM-TEST-055/062] Evidence applies only to its exact observed build."""
    scoped = dataclasses.replace(
        _qualifications("portal_go")[0], min_sdk=29, max_sdk=30, fingerprint_prefixes=("build/ok",)
    )
    assert scoped.covers(sdk=0, fingerprint="build/ok") is False
    assert scoped.covers(sdk=28, fingerprint="build/ok") is False
    assert scoped.covers(sdk=31, fingerprint="build/ok") is False
    assert scoped.covers(sdk=29, fingerprint="") is False
    assert scoped.covers(sdk=29, fingerprint="build/no") is False
    assert scoped.covers(sdk=29, fingerprint="BUILD/OK.1") is True
    assert dataclasses.replace(scoped, min_sdk=None, max_sdk=None, fingerprint_prefixes=()).covers() is True

    MutableQualification = dataclasses.make_dataclass("MutableQualification", [("model_key", str)])
    with pytest.raises(CatalogError, match="qualification .* mutable"):
        validate_catalog(_catalog(qualifications=(MutableQualification("portal_go"),)))

    unknown_model = dataclasses.replace(_qualifications("portal_go")[0], model_key="unknown")
    with pytest.raises(CatalogError, match="unknown device model"):
        validate_catalog(_catalog(qualifications=(unknown_model,)))
    unknown_scenario = dataclasses.replace(_qualifications("portal_go")[0], scenario="invented")
    with pytest.raises(CatalogError, match="undeclared scenario"):
        validate_catalog(_catalog(qualifications=(unknown_scenario,)))
    unknown_result = dataclasses.replace(_qualifications("portal_go")[0], result="invented")
    with pytest.raises(CatalogError, match="undeclared result"):
        validate_catalog(_catalog(qualifications=(unknown_result,)))

    proposed = dataclasses.replace(CATALOG.assignments[0], state=ASSIGNMENT_PROPOSED)
    validate_catalog(_catalog(assignments=(proposed,)))
    failed = dataclasses.replace(_qualifications("portal_go")[0], result="fail")
    validate_catalog(_catalog(qualifications=(failed,)))


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


def test_portal_go_and_mini_share_behavior_but_retain_exact_model_identity():
    """[KSM-TEST-257] Shared provisioning does not merge model identities."""
    go = resolve_catalog_entry(_PORTAL_GO)
    mini = resolve_catalog_entry(_PORTAL_MINI)
    assert go.model_key == "portal_go"
    assert mini.model_key == "portal_mini"
    assert go.recipe_identity == mini.recipe_identity == "meta_portal_android10_local_dns"
    assert go.executable is True


def test_sharing_a_recipe_never_shares_qualification_evidence():
    """KSM-TEST-057 negative control: Mini's evidence stays Mini's."""
    catalog = _catalog(qualifications=_qualifications("portal_mini"))
    go = resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29)
    mini = resolve_catalog_entry(_PORTAL_MINI, catalog=catalog, sdk=29)

    assert mini.support_state == SUPPORT_SUPPORTED
    assert go.support_state == SUPPORT_RECIPE_ASSIGNED


def test_portal_tv_shares_portal_provisioning_recipe():
    """[KSM-TEST-257] KSM leaves native Home behavior to KS on Portal TV too."""
    tv = resolve_catalog_entry(_PORTAL_TV)
    assert tv.model_key == "portal_tv"
    assert tv.recipe_identity == "meta_portal_tv_local_dns"


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
    with pytest.raises(device_catalog.NoApprovedRecipe, match="no approved recipe"):
        require_recipe("portal_go", catalog=_catalog(assignments=assignments))
    assert derive_support_state("not-a-model")[0] == SUPPORT_UNKNOWN


# --- KSM-TEST-061/062: derived support state --------------------------------


def test_full_matrix_yields_supported_only_for_the_qualified_model():
    """KSM-TEST-061."""
    catalog = _catalog(qualifications=_qualifications("portal_go"))
    assert resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29).support_state == (
        SUPPORT_SUPPORTED
    )
    assert resolve_catalog_entry(_PORTAL_MINI, catalog=catalog, sdk=29).support_state == (
        SUPPORT_RECIPE_ASSIGNED
    )


def test_a_partial_matrix_is_partially_qualified_not_supported():
    """KSM-TEST-061: evidence for some scenarios is not evidence for all."""
    partial = _qualifications("portal_go")[:2]
    catalog = _catalog(qualifications=partial)
    assert resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29).support_state == (
        SUPPORT_PARTIALLY_QUALIFIED
    )


def test_a_failed_scenario_blocks_the_model():
    quals = _qualifications("portal_go")[:-1] + (
        dataclasses.replace(_qualifications("portal_go")[-1], result="fail"),
    )
    catalog = _catalog(qualifications=quals)
    entry = resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29)
    assert entry.support_state == SUPPORT_BLOCKED


def test_evidence_for_a_previous_behavior_key_goes_stale():
    """[KSM-TEST-062] A changed behavior key does not inherit old evidence."""
    changed = dataclasses.replace(get_recipe("meta_portal_android10_local_dns"), recipe_key="meta_portal_next", start_url_path="/portal-next")
    assignments = tuple(
        dataclasses.replace(a, recipe_key="meta_portal_next") if a.model_key == "portal_go" else a
        for a in CATALOG.assignments
    )
    catalog = _catalog(
        recipes=CATALOG.recipes + (changed,),
        assignments=assignments,
        qualifications=_qualifications("portal_go"),
    )
    entry = resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29)
    assert entry.recipe_identity == "meta_portal_next"
    assert entry.support_state == SUPPORT_REVALIDATION_REQUIRED
    assert "meta_portal" in entry.reason


def test_evidence_outside_the_observed_build_scope_requires_revalidation():
    """KSM-TEST-062: an SDK outside the evidence's build scope is out of scope,
    not silently covered."""
    catalog = _catalog(qualifications=_qualifications("portal_go"))
    in_scope = resolve_catalog_entry(_PORTAL_GO, catalog=catalog, sdk=29)
    out_of_scope = resolve_catalog_entry(
        dataclasses.replace(_PORTAL_GO, sdk=33), catalog=catalog, sdk=33
    )
    assert in_scope.support_state == SUPPORT_SUPPORTED
    assert out_of_scope.support_state == SUPPORT_REVALIDATION_REQUIRED
    assert "build scope" in out_of_scope.reason


def test_required_scenarios_do_not_include_native_home_selection():
    required = required_scenarios(get_recipe("meta_portal_android10_local_dns"))
    assert "launcher_selection" not in required
    assert {"clean_install", "existing_reuse", "update", "reinstall", "uninstall"} <= required


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


_PORTAL_GO_40_FINGERPRINT = (
    "Facebook/terry_prod/terry:10/QKQ1.210213.001/5051355900018050:user/prod-keys"
)


def test_KSM_TEST_304_portal_go_full_matrix_on_the_renamed_recipe_is_supported():
    """[KSM-TEST-304] KSM-BEHAVE-151: with no undeclared grant required, the
    live #120 Go matrix makes that exact build supported. The evidence stays
    scoped to the build, the model and the new recipe key."""
    observed = dataclasses.replace(_PORTAL_GO, fingerprint=_PORTAL_GO_40_FINGERPRINT)
    entry = resolve_catalog_entry(observed)
    assert entry.recipe.recipe_key == "meta_portal_android10_local_dns"
    assert "android.permission.WRITE_SECURE_SETTINGS" not in entry.recipe.permissions_for_sdk(29)
    records = [q for q in CATALOG.qualifications if q.model_key == "portal_go"]
    assert {q.scenario for q in records} == required_scenarios(entry.recipe)
    assert {q.result for q in records} == {"pass"}
    for record in records:
        assert record.recipe_key == "meta_portal_android10_local_dns"
        assert "2026.10.3" in record.evidence and "#121" in record.evidence
        assert record.positive_control and record.negative_control
        assert record.min_sdk == record.max_sdk == 29
        assert record.fingerprint_prefixes == (_PORTAL_GO_40_FINGERPRINT.lower(),)
    assert entry.support_state == SUPPORT_SUPPORTED
    for facts in (
        _PORTAL_GO,
        dataclasses.replace(observed, fingerprint="another/build"),
        dataclasses.replace(observed, sdk=30),
    ):
        assert resolve_catalog_entry(facts).support_state == SUPPORT_REVALIDATION_REQUIRED
    assert resolve_catalog_entry(_PORTAL_MINI).support_state == SUPPORT_RECIPE_ASSIGNED
    # No record anywhere still names a retired recipe key.
    assert not {q.recipe_key for q in CATALOG.qualifications} & {
        "meta_portal_android10", "meta_portal_android9", "meta_portal_tv",
        "meta_portal_android10_declared_grants", "meta_portal_android9_declared_grants",
        "meta_portal_tv_declared_grants",
    }
    # A later behavior change (new key) cannot inherit this evidence.
    new_recipe = dataclasses.replace(entry.recipe, recipe_key="meta_portal_next")
    assignments = tuple(
        dataclasses.replace(a, recipe_key="meta_portal_next")
        if a.model_key == "portal_go" and a.state == ASSIGNMENT_APPROVED else a
        for a in CATALOG.assignments
    )
    changed = _catalog(recipes=CATALOG.recipes + (new_recipe,), assignments=assignments)
    assert resolve_catalog_entry(observed, catalog=changed).support_state == SUPPORT_REVALIDATION_REQUIRED


def test_KSM_TEST_305_private_dns_recipes_need_the_private_dns_scenario():
    """[KSM-TEST-305] KSM-BEHAVE-152: a recipe that turns Private DNS off is not
    `supported` on any build until the hardware matrix proved the change and its
    undo; recipes that never touch Private DNS do not need the scenario."""
    for recipe in CATALOG.recipes:
        assert ("private_dns" in required_scenarios(recipe)) is recipe.disables_private_dns, recipe.recipe_key
    assert any(not recipe.disables_private_dns for recipe in CATALOG.recipes)  # control
    observed = dataclasses.replace(_PORTAL_GO, fingerprint=_PORTAL_GO_40_FINGERPRINT)
    assert resolve_catalog_entry(observed).support_state == SUPPORT_SUPPORTED  # control
    without = tuple(
        q for q in CATALOG.qualifications
        if not (q.model_key == "portal_go" and q.scenario == "private_dns")
    )
    assert len(without) == len(CATALOG.qualifications) - 1
    assert resolve_catalog_entry(observed, catalog=_catalog(qualifications=without)).support_state != SUPPORT_SUPPORTED


_PLUS_FIXTURE = Path(__file__).parents[1] / "fixtures/profile-portal-plus-gen2-2026-10-02.json"


def test_KSM_TEST_297_portal_plus_gen2_records_its_own_live_matrix():
    """[KSM-TEST-297] The live Portal+ Gen 2 run resolves to its exact model and
    derives its state from its own build-scoped records only (#115; the
    records were re-run on the renamed recipe in #120)."""
    fixture = json.loads(_PLUS_FIXTURE.read_text())
    platform = fixture["facts"]["platform"]
    observed = DeviceFacts.from_platform(platform)
    entry = resolve_catalog_entry(observed)
    assert entry.model_key == fixture["expected_model_key"] == "portal_plus_gen2"
    assert entry.recipe.recipe_key == "meta_portal_android10_local_dns"

    records = [q for q in CATALOG.qualifications if q.model_key == "portal_plus_gen2"]
    by_scenario = {q.scenario: q.result for q in records}
    assert set(by_scenario) == required_scenarios(entry.recipe)
    assert set(by_scenario.values()) == {"pass"}
    for record in records:
        assert record.min_sdk == record.max_sdk == 29
        assert record.fingerprint_prefixes == (platform["fingerprint"].lower(),)
        assert "2026.10.3" in record.evidence
        assert record.positive_control and record.negative_control
    assert entry.support_state == SUPPORT_SUPPORTED

    # Negative controls: an unobserved build, the Gen 1 Portal+ and the
    # sibling Portal Go never inherit this evidence.
    for facts in (
        dataclasses.replace(observed, fingerprint="facebook/cipher_prod/cipher:10/other"),
        dataclasses.replace(observed, sdk=30),
    ):
        assert resolve_catalog_entry(facts).support_state == SUPPORT_REVALIDATION_REQUIRED
    gen1 = resolve_catalog_entry(dataclasses.replace(observed, sdk=28))
    assert gen1.model_key == "portal_plus_gen1"
    assert gen1.support_state == SUPPORT_RECIPE_ASSIGNED
    go = [q for q in CATALOG.qualifications if q.model_key == "portal_go"]
    assert all(q.fingerprint_prefixes != records[0].fingerprint_prefixes for q in go)


@pytest.mark.parametrize("result", ["fail", "blocked"])
def test_failed_qualification_does_not_leak_across_builds(result):
    """[KSM-TEST-124] Apply the scope before interpreting any outcome."""
    failure = dataclasses.replace(
        _qualifications("portal_go")[0], result=result, fingerprint_prefixes=("bad/build",)
    )
    passing = tuple(dataclasses.replace(q, fingerprint_prefixes=("good/build",))
                    for q in _qualifications("portal_go"))
    catalog = _catalog(qualifications=(failure,) + passing)
    assert resolve_catalog_entry(
        dataclasses.replace(_PORTAL_GO, fingerprint="good/build"), catalog=catalog
    ).support_state == SUPPORT_SUPPORTED
    assert resolve_catalog_entry(
        dataclasses.replace(_PORTAL_GO, fingerprint="bad/build"), catalog=catalog
    ).support_state == SUPPORT_BLOCKED
    assert resolve_catalog_entry(_PORTAL_GO, catalog=catalog).support_state == SUPPORT_REVALIDATION_REQUIRED
    overlapping = _catalog(qualifications=(failure,) + _qualifications("portal_go"))
    assert resolve_catalog_entry(
        dataclasses.replace(_PORTAL_GO, fingerprint="bad/build"), catalog=overlapping
    ).support_state == SUPPORT_BLOCKED
