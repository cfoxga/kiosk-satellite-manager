"""The source-maintained device catalog (KSM-BEHAVE-047/049/050, issue #20).

Four logical tables, all frozen Python source data under this package:

* device models   -- `device_models.DEVICE_MODELS` (exact hardware identity)
* install recipes -- `install_recipes.INSTALL_RECIPES` (behavior keys)
* assignments     -- `RECIPE_ASSIGNMENTS` (which model may use which version)
* qualifications  -- `QUALIFICATIONS` (per-model, per-version, per-build evidence)

There is no SQL, no runtime database, no Home Assistant state store and no
administration UI. Physical devices remain config entries / device-registry
devices; nothing here is per-unit inventory.

Resolution is three independent steps, in order, each of which can fail closed
on its own:

1. exact model     -- `device_models.match_device_model`
2. approved recipe -- `_approved_assignment` + `_find_recipe`
3. support state   -- `derive_support_state`

Step 3 never feeds step 2: an unqualified model still provisions with its
approved recipe (that is how a model gets qualified in the first place); what
it does not get is a *supported* claim. Conversely a model with no approved
assignment gets no executable recipe at all, however much its sibling is
qualified -- sharing a recipe never shares evidence (KSM-BEHAVE-050/052).
"""
from __future__ import annotations

from dataclasses import dataclass

from .device_models import (
    DEVICE_MODELS,
    DeviceFacts,
    DeviceModel,
    FallbackClassification,
    classify_fallback,
    match_device_model,
)
from .install_recipes import INSTALL_RECIPES, InstallRecipe, validate_recipe

ASSIGNMENT_PROPOSED = "proposed"
ASSIGNMENT_APPROVED = "approved"
ASSIGNMENT_RETIRED = "retired"
ASSIGNMENT_STATES = frozenset({ASSIGNMENT_PROPOSED, ASSIGNMENT_APPROVED, ASSIGNMENT_RETIRED})

RESULT_PASS = "pass"
RESULT_FAIL = "fail"
RESULT_BLOCKED = "blocked"
RESULTS = frozenset({RESULT_PASS, RESULT_FAIL, RESULT_BLOCKED})

SCENARIO_CLEAN_INSTALL = "clean_install"
SCENARIO_EXISTING_REUSE = "existing_reuse"
SCENARIO_UPDATE = "update"
SCENARIO_REINSTALL = "reinstall"
SCENARIO_PERMISSION_CONVERGENCE = "permission_convergence"
SCENARIO_HEALTH_VERSION_READBACK = "health_version_readback"
SCENARIO_UNINSTALL = "uninstall"
SCENARIOS = frozenset(
    {
        SCENARIO_CLEAN_INSTALL,
        SCENARIO_EXISTING_REUSE,
        SCENARIO_UPDATE,
        SCENARIO_REINSTALL,
        SCENARIO_PERMISSION_CONVERGENCE,
        SCENARIO_HEALTH_VERSION_READBACK,
        SCENARIO_UNINSTALL,
    }
)

# Derived, never hand-maintained. There is deliberately no `supported=True`
# field anywhere in this module (KSM-BEHAVE-050).
SUPPORT_UNKNOWN = "unknown"
SUPPORT_RECOGNIZED = "recognized"
SUPPORT_RECIPE_ASSIGNED = "recipe_assigned"
SUPPORT_PARTIALLY_QUALIFIED = "partially_qualified"
SUPPORT_SUPPORTED = "supported"
SUPPORT_REVALIDATION_REQUIRED = "revalidation_required"
SUPPORT_BLOCKED = "blocked"


class CatalogError(ValueError):
    """The source catalog violates an integrity or safety invariant."""


class NoApprovedRecipe(Exception):
    """No approved recipe covers this device, so nothing may run.

    Raised instead of falling back to a default recipe. Before the catalog, an
    unmatched device received the Portal permission/Device Admin set on no
    evidence whatsoever; that fallback is what this exception replaces
    (KSM-BEHAVE-048).
    """


@dataclass(frozen=True)
class RecipeAssignment:
    """One model/recipe relationship, with its history preserved.

    A retired or proposed row is kept rather than deleted: which recipe a model
    used to be approved for is what makes a later `revalidation_required`
    legible.
    """

    model_key: str
    recipe_key: str
    state: str
    effective_date: str
    rationale: str

    @property
    def recipe_identity(self) -> str:
        return self.recipe_key


@dataclass(frozen=True)
class QualificationRecord:
    """One scenario's evidence for one model, recipe key and build scope.

    `min_sdk`/`max_sdk`/`fingerprint_prefixes` are the build scope. An empty
    scope means "unscoped", which is only honest for evidence that genuinely
    cannot depend on the build; every scoped record refuses to cover a device
    whose SDK was not observed at all.
    """

    model_key: str
    recipe_key: str
    scenario: str
    result: str
    verified_on: str
    evidence: str
    positive_control: str = ""
    negative_control: str = ""
    min_sdk: int | None = None
    max_sdk: int | None = None
    fingerprint_prefixes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    rollback_notes: str = ""

    @property
    def recipe_identity(self) -> str:
        return self.recipe_key

    def covers(self, sdk: int = 0, fingerprint: str = "") -> bool:
        """True when this evidence applies to the build actually in front of us."""
        if self.min_sdk is not None or self.max_sdk is not None:
            if sdk <= 0:
                return False
            if self.min_sdk is not None and sdk < self.min_sdk:
                return False
            if self.max_sdk is not None and sdk > self.max_sdk:
                return False
        if self.fingerprint_prefixes:
            observed = (fingerprint or "").strip().lower()
            if not observed or not any(
                observed.startswith(prefix) for prefix in self.fingerprint_prefixes
            ):
                return False
        return True


@dataclass(frozen=True)
class Catalog:
    """The five source tables as one immutable bundle.

    Bundled so a test can substitute a deliberately broken catalog without
    monkey-patching module globals, and so every resolver takes its data as an
    argument rather than reaching for a singleton.
    """

    models: tuple[DeviceModel, ...]
    recipes: tuple[InstallRecipe, ...]
    assignments: tuple[RecipeAssignment, ...]
    qualifications: tuple[QualificationRecord, ...]
    recovery_profile_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogResolution:
    """What the catalog can say about one device, facts and inference separated.

    `model_key`/`classification` are what was *identified*; `support_state` is
    what was *derived*. A caller that wants to act reads `executable`, which is
    true only for an approved assignment to a real recipe.
    """

    model_key: str | None
    model_name: str | None
    classification: str | None
    classification_name: str | None
    recipe: InstallRecipe | None
    assignment_state: str | None
    support_state: str
    reason: str
    evidence_refs: tuple[str, ...] = ()

    @property
    def recipe_identity(self) -> str | None:
        return self.recipe.identity if self.recipe else None

    @property
    def executable(self) -> bool:
        return self.recipe is not None and self.assignment_state == ASSIGNMENT_APPROVED

    def as_report(self) -> dict:
        """The sanitized shape the capability report and onboarding plan expose."""
        return {
            "model_key": self.model_key,
            "model_name": self.model_name,
            "classification": self.classification,
            "recipe_key": self.recipe.recipe_key if self.recipe else None,
            "assignment_state": self.assignment_state,
            "support_state": self.support_state,
            "reason": self.reason,
            "executable_recipe": self.executable,
        }


RECIPE_ASSIGNMENTS: tuple[RecipeAssignment, ...] = (
    RecipeAssignment(
        model_key="onn_4k_pro_android14",
        recipe_key="onn_4k_pro_android14",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-29",
        rationale="Exact Theater and Great Room GTV identity confirmed by live ADB reads; "
        "qualification remains per build and scenario.",
    ),
    # KSM-BEHAVE-136: Android 9's confirmed repurpose capability is distinct.
    # Native KS settings still own Home. Qualification stays model-specific.
    RecipeAssignment(
        model_key="portal_go",
        recipe_key="meta_portal_android10_declared_grants",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-22",
        rationale="Portal provisioning without KSM Home control (KSM-BEHAVE-132).",
    ),
    RecipeAssignment(
        model_key="portal_mini",
        recipe_key="meta_portal_android10_declared_grants",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale="Shares Portal install behavior, not qualification evidence.",
    ),
    RecipeAssignment(
        model_key="portal_gen1",
        recipe_key="meta_portal_android9_declared_grants",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale="Shares Portal install behavior without KSM Home control.",
    ),
    RecipeAssignment(
        model_key="portal_gen2",
        recipe_key="meta_portal_android10_declared_grants",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-27",
        rationale="Shares Portal install behavior without KSM Home control.",
    ),
    RecipeAssignment(
        model_key="portal_plus_gen1",
        recipe_key="meta_portal_android9_declared_grants",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale="Behavior migrated verbatim from the portal_plus_gen1 DeviceProfile.",
    ),
    RecipeAssignment(
        model_key="portal_plus_gen2",
        recipe_key="meta_portal_android10_declared_grants",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale="Behavior migrated verbatim from the portal_plus_gen2 DeviceProfile.",
    ),
    RecipeAssignment(
        model_key="portal_tv",
        recipe_key="meta_portal_tv_declared_grants",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale="Shares Portal install behavior; KS owns native Home settings.",
    ),
)

# KSM-BEHAVE-151 (#120): the Portal recipes stopped requiring a grant Kiosk
# Satellite never declares, so they got new keys and the evidence recorded
# against the retired keys (#40/#41, #115) was dropped. These are the full
# physical matrix runs on the renamed recipe, against dev HA, 2026-10-02.
_MATRIX_SCENARIOS: tuple[tuple[str, str, str, str], ...] = (
    (
        SCENARIO_CLEAN_INSTALL,
        "fresh install through a new KSM entry reached a GREEN e2e result.",
        "Package and KSM entry present after setup.",
        "The scenario fails when the package or entry is missing.",
    ),
    (
        SCENARIO_EXISTING_REUSE,
        "setup reused the installed app without reinstalling.",
        "Existing package kept and entry created.",
        "The scenario fails when reuse does not complete setup.",
    ),
    (
        SCENARIO_UPDATE,
        "the Install button reinstalled the target release.",
        "Install button press completed with the app running.",
        "The scenario fails when the button is unavailable or the install errors.",
    ),
    (
        SCENARIO_REINSTALL,
        "setup with existing_install_action=reinstall completed.",
        "Entry created after a reinstall.",
        "The scenario fails when the reinstall path does not finish setup.",
    ),
    (
        SCENARIO_PERMISSION_CONVERGENCE,
        "every runtime permission the recipe requires read back granted=true, with the "
        "overlay AppOp allowed and the battery exemption listed.",
        "READ_LOGS readback shows granted=true in the same package dump.",
        "BLUETOOTH_SCAN, outside the SDK-29 set, is not granted; a missing grant fails.",
    ),
    (
        SCENARIO_HEALTH_VERSION_READBACK,
        "Android package versionName matched KS /api/health appVersion.",
        "Non-empty versions agreed; post-run dumpsys reads versionName 2026.10.3.",
        "The scenario fails on any mismatch between the two readbacks.",
    ),
    (
        SCENARIO_UNINSTALL,
        "the KSM Uninstall button removed me.jxl.kiosk_satellite.",
        "Package installed and the button available before the press.",
        "The scenario fails when the package is still installed after the press.",
    ),
)


def _matrix_records(model_key: str, device: str, fingerprint: str) -> tuple[QualificationRecord, ...]:
    return tuple(
        QualificationRecord(
            model_key=model_key,
            recipe_key="meta_portal_android10_declared_grants",
            scenario=scenario,
            result=RESULT_PASS,
            verified_on="2026-10-02",
            min_sdk=29,
            max_sdk=29,
            fingerprint_prefixes=(fingerprint,),
            evidence=(
                f"KS 2026.10.3 (versionCode 301) on {device}, SDK 29: {evidence} "
                "Physical matrix via kiosk-satellite-manager/scripts/test-device-matrix.py "
                "against dev HA, KSM issue #120."
            ),
            positive_control=positive,
            negative_control=negative,
            limitations=(
                "Evidence applies to the observed KS app build; revalidate after an app update.",
                "Other Portal SKUs, Device Owner and Test Harness are not covered.",
            ),
            rollback_notes="Teardown wiped the device; KSM restored its exported config and HA entry.",
        )
        for scenario, evidence, positive, negative in _MATRIX_SCENARIOS
    )


QUALIFICATIONS: tuple[QualificationRecord, ...] = _matrix_records(
    "portal_go",
    "Test Portal Go / terry_prod",
    "facebook/terry_prod/terry:10/qkq1.210213.001/5051355900018050:user/prod-keys",
) + _matrix_records(
    "portal_plus_gen2",
    "Test Portal Plus / cipher_prod",
    "facebook/cipher_prod/cipher:10/qkq1.210213.001/4051355900018050:user/prod-keys",
)

_RECOVERY_QUALIFIED_MODEL_KEYS: tuple[str, ...] = (
    "portal_go",
    "portal_mini",
    "portal_gen1",
    "portal_gen2",
    "portal_plus_gen1",
    "portal_plus_gen2",
    "portal_tv",
)

CATALOG = Catalog(
    models=DEVICE_MODELS,
    recipes=INSTALL_RECIPES,
    assignments=RECIPE_ASSIGNMENTS,
    qualifications=QUALIFICATIONS,
    recovery_profile_keys=_RECOVERY_QUALIFIED_MODEL_KEYS,
)


def required_scenarios(recipe: InstallRecipe) -> frozenset[str]:
    """The matrix a model must pass on this recipe to be `supported`."""
    required = {
        SCENARIO_CLEAN_INSTALL,
        SCENARIO_EXISTING_REUSE,
        SCENARIO_UPDATE,
        SCENARIO_REINSTALL,
        SCENARIO_PERMISSION_CONVERGENCE,
        SCENARIO_HEALTH_VERSION_READBACK,
        SCENARIO_UNINSTALL,
    }
    return frozenset(required)


def _find_recipe(catalog: Catalog, recipe_key: str) -> InstallRecipe | None:
    for recipe in catalog.recipes:
        if recipe.recipe_key == recipe_key:
            return recipe
    return None


def _find_model(catalog: Catalog, model_key: str | None) -> DeviceModel | None:
    if not model_key:
        return None
    for model in catalog.models:
        if model.model_key == model_key:
            return model
    return None


def _approved_assignment(catalog: Catalog, model_key: str) -> RecipeAssignment | None:
    for assignment in catalog.assignments:
        if assignment.model_key == model_key and assignment.state == ASSIGNMENT_APPROVED:
            return assignment
    return None


def validate_catalog(catalog: Catalog = CATALOG) -> None:
    """Raise `CatalogError` unless every invariant in KSM-BEHAVE-047/049 holds.

    Called at import time by `__init__.py`, so a bad data contribution fails
    the integration's setup loudly instead of resolving something plausible.
    """
    seen_models: set[str] = set()
    for model in catalog.models:
        if not model.__dataclass_params__.frozen:
            raise CatalogError(f"device model {model.model_key} is mutable")
        if model.model_key in seen_models:
            raise CatalogError(f"duplicate device model key {model.model_key!r}")
        seen_models.add(model.model_key)

    seen_recipes: set[str] = set()
    for recipe in catalog.recipes:
        if not recipe.__dataclass_params__.frozen:
            raise CatalogError(f"install recipe {recipe.identity} is mutable")
        if recipe.identity in seen_recipes:
            raise CatalogError(f"duplicate install recipe {recipe.identity!r}")
        seen_recipes.add(recipe.identity)
        validate_recipe(recipe)

    approved_seen: set[str] = set()
    for assignment in catalog.assignments:
        if not assignment.__dataclass_params__.frozen:
            raise CatalogError(f"assignment for {assignment.model_key} is mutable")
        if assignment.state not in ASSIGNMENT_STATES:
            raise CatalogError(
                f"assignment {assignment.model_key} -> {assignment.recipe_identity} has "
                f"undeclared state {assignment.state!r}"
            )
        model = _find_model(catalog, assignment.model_key)
        if model is None:
            raise CatalogError(
                f"assignment references unknown device model {assignment.model_key!r}"
            )
        recipe = _find_recipe(catalog, assignment.recipe_key)
        if recipe is None:
            raise CatalogError(
                f"assignment for {assignment.model_key!r} references unknown install "
                f"recipe {assignment.recipe_identity!r}"
            )
        if assignment.state != ASSIGNMENT_APPROVED:
            continue
        if assignment.model_key in approved_seen:
            raise CatalogError(
                f"device model {assignment.model_key!r} has more than one approved recipe "
                f"assignment; exactly one recipe may be executable"
            )
        approved_seen.add(assignment.model_key)

    for record in catalog.qualifications:
        if not record.__dataclass_params__.frozen:
            raise CatalogError(f"qualification for {record.model_key} is mutable")
        if _find_model(catalog, record.model_key) is None:
            raise CatalogError(
                f"qualification references unknown device model {record.model_key!r}"
            )
        if _find_recipe(catalog, record.recipe_key) is None:
            raise CatalogError(
                f"qualification for {record.model_key!r} references unknown install "
                f"recipe {record.recipe_identity!r}"
            )
        if record.scenario not in SCENARIOS:
            raise CatalogError(f"qualification names undeclared scenario {record.scenario!r}")
        if record.result not in RESULTS:
            raise CatalogError(f"qualification has undeclared result {record.result!r}")
        # Support is *derived* from these records, so an unevidenced or
        # unscoped row is the hand-maintained "supported: true" boolean this
        # module exists to remove, wearing a dataclass. A passing record must
        # say what was observed and on which builds it was observed; `covers()`
        # short-circuits to True for an empty scope, so an unscoped pass would
        # otherwise qualify a device whose SDK was never read at all.
        if record.result == RESULT_PASS:
            if not record.evidence.strip():
                raise CatalogError(
                    f"qualification {record.model_key}/{record.scenario} on "
                    f"{record.recipe_identity} passes with no evidence recorded"
                )
            if record.min_sdk is None and record.max_sdk is None and not record.fingerprint_prefixes:
                raise CatalogError(
                    f"qualification {record.model_key}/{record.scenario} on "
                    f"{record.recipe_identity} passes with no build scope; record the "
                    f"SDK range or fingerprint prefixes the run actually covered"
                )

    for key in catalog.recovery_profile_keys:
        if _find_model(catalog, key) is None:
            raise CatalogError(
                f"recovery profile {key!r} references unknown device model; recovery "
                f"evidence is per exact model and can never key on a fallback "
                f"classification (KSM-BEHAVE-052)"
            )


def require_recipe(model_key: str | None, *, catalog: Catalog = CATALOG) -> InstallRecipe:
    """The approved recipe for an exact model, or raise `NoApprovedRecipe`.

    The one entry point a provisioning executor may use. It takes a model key
    rather than facts because the config entry already stores the model that
    was matched at setup time; re-matching here would let a device's identity
    drift silently change what gets executed on it.
    """
    model = _find_model(catalog, model_key)
    if model is None:
        raise NoApprovedRecipe(
            f"{model_key!r} is not an exact device model in the catalog; a fallback "
            f"classification never receives an executable recipe"
        )
    assignment = _approved_assignment(catalog, model.model_key)
    if assignment is None:
        raise NoApprovedRecipe(
            f"{model.model_key!r} has no approved recipe assignment; add one with its "
            f"rationale before provisioning this model"
        )
    recipe = _find_recipe(catalog, assignment.recipe_key)
    if recipe is None:  # pragma: no cover -- validate_catalog rejects this
        raise NoApprovedRecipe(
            f"approved assignment {assignment.recipe_identity!r} names a recipe "
            f"that does not exist"
        )
    return recipe


def derive_support_state(
    model_key: str | None,
    *,
    catalog: Catalog = CATALOG,
    sdk: int = 0,
    fingerprint: str = "",
) -> tuple[str, str]:
    """Derive (state, reason) from evidence alone. Never a stored boolean."""
    model = _find_model(catalog, model_key)
    if model is None:
        return (
            SUPPORT_UNKNOWN,
            "no exact device model matched the observed identity facts",
        )

    assignment = _approved_assignment(catalog, model.model_key)
    if assignment is None:
        proposed = [
            a.recipe_identity
            for a in catalog.assignments
            if a.model_key == model.model_key and a.state == ASSIGNMENT_PROPOSED
        ]
        detail = (
            f"; proposed but unapproved: {', '.join(proposed)}" if proposed else ""
        )
        return (
            SUPPORT_RECOGNIZED,
            f"{model.model_key} is a known model with no approved recipe assignment{detail}",
        )

    recipe = _find_recipe(catalog, assignment.recipe_key)
    if recipe is None:  # pragma: no cover -- validate_catalog rejects this
        return (
            SUPPORT_BLOCKED,
            f"approved assignment {assignment.recipe_identity} names a recipe "
            f"that does not exist",
        )

    for_model = [q for q in catalog.qualifications if q.model_key == model.model_key]
    current = [q for q in for_model if q.recipe_key == assignment.recipe_key]
    if not current and not for_model:
        return (
            SUPPORT_RECIPE_ASSIGNED,
            f"{model.model_key} is approved for {assignment.recipe_identity} but has no "
            f"qualification evidence of its own on file",
        )

    if not current:
        stale = ", ".join(sorted({q.recipe_key for q in for_model}))
        return (
            SUPPORT_REVALIDATION_REQUIRED,
            f"qualification evidence for {model.model_key} covers {stale} only; "
            f"{assignment.recipe_identity} has none",
        )

    in_scope = [q for q in current if q.covers(sdk, fingerprint)]
    if not in_scope:
        return (
            SUPPORT_REVALIDATION_REQUIRED,
            f"the observed build (sdk={sdk or 'unobserved'}) is outside the build scope of "
            f"every qualification record for {model.model_key} on "
            f"{assignment.recipe_identity}",
        )

    failed = sorted({q.scenario for q in in_scope if q.result != RESULT_PASS})
    if failed:
        return (
            SUPPORT_BLOCKED,
            f"{model.model_key} has a failing or blocked scenario on "
            f"{assignment.recipe_identity}: {', '.join(failed)}",
        )

    required = required_scenarios(recipe)
    passed = {q.scenario for q in in_scope if q.result == RESULT_PASS}
    missing = sorted(required - passed)
    if missing:
        return (
            SUPPORT_PARTIALLY_QUALIFIED,
            f"{model.model_key} on {assignment.recipe_identity} is missing evidence for "
            f"{', '.join(missing)}",
        )
    return (
        SUPPORT_SUPPORTED,
        f"{model.model_key} passes the full required matrix for "
        f"{assignment.recipe_identity} within the observed build scope",
    )


def resolve_catalog_entry(
    facts: DeviceFacts,
    *,
    catalog: Catalog = CATALOG,
    sdk: int | None = None,
    fingerprint: str | None = None,
) -> CatalogResolution:
    """Identify the device, then -- only then -- resolve an executable recipe.

    Returns a resolution in every case; an unmatched or unassigned device gets
    `recipe=None`, which callers must treat as "do not provision" rather than
    substituting a default (KSM-BEHAVE-048).
    """
    effective_sdk = facts.sdk if sdk is None else sdk
    effective_fingerprint = facts.fingerprint if fingerprint is None else fingerprint

    model = match_device_model(facts, catalog.models)
    if model is None:
        classification: FallbackClassification = classify_fallback(facts)
        return CatalogResolution(
            model_key=None,
            model_name=None,
            classification=classification.key,
            classification_name=classification.name,
            recipe=None,
            assignment_state=None,
            support_state=SUPPORT_UNKNOWN,
            reason=(
                f"no exact device model matched the observed identity facts; classified "
                f"as {classification.key}. {classification.reason}"
            ),
        )

    assignment = _approved_assignment(catalog, model.model_key)
    recipe = (
        _find_recipe(catalog, assignment.recipe_key)
        if assignment
        else None
    )
    support_state, reason = derive_support_state(
        model.model_key,
        catalog=catalog,
        sdk=effective_sdk,
        fingerprint=effective_fingerprint,
    )
    return CatalogResolution(
        model_key=model.model_key,
        model_name=model.name,
        classification=None,
        classification_name=None,
        recipe=recipe,
        assignment_state=assignment.state if assignment else None,
        support_state=support_state,
        reason=reason,
        evidence_refs=model.evidence_refs,
    )
