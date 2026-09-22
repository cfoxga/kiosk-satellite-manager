"""The source-maintained device catalog (KSM-BEHAVE-047/049/050, issue #20).

Four logical tables, all frozen Python source data under this package:

* device models   -- `device_models.DEVICE_MODELS` (exact hardware identity)
* install recipes -- `install_recipes.INSTALL_RECIPES` (versioned behavior)
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
SCENARIO_LAUNCHER_SELECTION = "launcher_selection"
SCENARIO_HEALTH_VERSION_READBACK = "health_version_readback"
SCENARIO_UNINSTALL = "uninstall"
SCENARIOS = frozenset(
    {
        SCENARIO_CLEAN_INSTALL,
        SCENARIO_EXISTING_REUSE,
        SCENARIO_UPDATE,
        SCENARIO_REINSTALL,
        SCENARIO_PERMISSION_CONVERGENCE,
        SCENARIO_LAUNCHER_SELECTION,
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
    """No approved recipe version covers this device, so nothing may run.

    Raised instead of falling back to a default recipe. Before the catalog, an
    unmatched device received the Portal permission/Device Admin set on no
    evidence whatsoever; that fallback is what this exception replaces
    (KSM-BEHAVE-048).
    """


@dataclass(frozen=True)
class RecipeAssignment:
    """One versioned model/recipe relationship, with its history preserved.

    A retired or proposed row is kept rather than deleted: which recipe a model
    used to be approved for is what makes a later `revalidation_required`
    legible.
    """

    model_key: str
    recipe_key: str
    recipe_version: str
    state: str
    effective_date: str
    rationale: str

    @property
    def recipe_identity(self) -> str:
        return f"{self.recipe_key}:{self.recipe_version}"


@dataclass(frozen=True)
class QualificationRecord:
    """One scenario's evidence for one model, recipe version and build scope.

    `min_sdk`/`max_sdk`/`fingerprint_prefixes` are the build scope. An empty
    scope means "unscoped", which is only honest for evidence that genuinely
    cannot depend on the build; every scoped record refuses to cover a device
    whose SDK was not observed at all.
    """

    model_key: str
    recipe_key: str
    recipe_version: str
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
        return f"{self.recipe_key}:{self.recipe_version}"

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
    true only for an approved assignment to a real recipe version.
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
            "recipe_version": self.recipe.version if self.recipe else None,
            "assignment_state": self.assignment_state,
            "support_state": self.support_state,
            "reason": self.reason,
            "executable_recipe": self.executable,
        }


RECIPE_ASSIGNMENTS: tuple[RecipeAssignment, ...] = (
    # Migrated 1:1 from the pre-catalog DeviceProfile records: every Portal
    # model below already received exactly this behavior before the split, so
    # approving the assignment preserves shipped behavior rather than
    # extending it (issue #20 Acceptance: "Current Portal Go provisioning
    # remains behaviorally unchanged"). What is NOT carried over is any support
    # claim -- see QUALIFICATIONS.
    RecipeAssignment(
        model_key="portal_go",
        recipe_key="meta_portal_standard",
        recipe_version="v3",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-22",
        rationale=(
            "Portal Go's observed Android 10 build keeps Meta DeviceSetupActivity "
            "as HOME even after KS enables HomeAlias and package manager reports "
            "set-home-activity Success. v3 removes launcher takeover from this "
            "exact model's required behavior; sibling Portal models retain v2 "
            "until their own live evidence says otherwise (issue #41)."
        ),
    ),
    RecipeAssignment(
        model_key="portal_mini",
        recipe_key="meta_portal_standard",
        recipe_version="v2",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale=(
            "Same Android 10 'omni' Portal platform and identical pre-catalog "
            "DeviceProfile behavior. Shares the recipe; shares no evidence -- "
            "its live evidence is Test Harness recovery only."
        ),
    ),
    RecipeAssignment(
        model_key="portal_gen1",
        recipe_key="meta_portal_standard",
        recipe_version="v2",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale="Behavior migrated verbatim from the portal_gen1 DeviceProfile.",
    ),
    RecipeAssignment(
        model_key="portal_gen2",
        recipe_key="meta_portal_standard",
        recipe_version="v2",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale="Behavior migrated verbatim from the portal_gen2 DeviceProfile.",
    ),
    RecipeAssignment(
        model_key="portal_plus_gen1",
        recipe_key="meta_portal_standard",
        recipe_version="v2",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale="Behavior migrated verbatim from the portal_plus_gen1 DeviceProfile.",
    ),
    RecipeAssignment(
        model_key="portal_plus_gen2",
        recipe_key="meta_portal_standard",
        recipe_version="v2",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale="Behavior migrated verbatim from the portal_plus_gen2 DeviceProfile.",
    ),
    RecipeAssignment(
        model_key="portal_tv",
        recipe_key="meta_portal_tv",
        recipe_version="v2",
        state=ASSIGNMENT_APPROVED,
        effective_date="2026-09-20",
        rationale=(
            "home_launcher_supported=False is behaviorally significant, so "
            "Portal TV cannot share the standard Portal recipe (issue #20)."
        ),
    ),
)

# Exact-device negative qualification from issues #40/#41. This is installed-app
# and OEM-build evidence, not a claim about sibling Portal hardware. The issue
# #41 live run re-exercised the unchanged permission policy on v3 while proving
# the OEM HOME resolver cannot safely be replaced on this build.
QUALIFICATIONS: tuple[QualificationRecord, ...] = (
    QualificationRecord(
        model_key="portal_go",
        recipe_key="meta_portal_standard",
        recipe_version="v3",
        scenario=SCENARIO_PERMISSION_CONVERGENCE,
        result=RESULT_FAIL,
        verified_on="2026-09-22",
        min_sdk=29,
        max_sdk=29,
        fingerprint_prefixes=(
            "facebook/terry_prod/terry:10/qkq1.210213.001/5051355900018050:user/prod-keys",
        ),
        evidence=(
            "KS 2026.9.70 (versionCode 269) does not request WRITE_SECURE_SETTINGS; "
            "Android rejects pm grant with 'has not requested permission'. "
            "Issue #41's live v3 candidate reproduced the same sole denied grant. "
            "See docs/developer/android-support/portal-go-permission-qualification.md "
            "in ham-harness/kiosk-satellite-manager and KSM issue #40."
        ),
        positive_control="READ_LOGS grant and Android granted=true readback succeed.",
        negative_control=(
            "WRITE_SECURE_SETTINGS grant is rejected and remains absent from granted "
            "permissions; the physical matrix permission assertion returns false."
        ),
        limitations=(
            "Required secure-settings permission is not converged; no supported claim.",
            "Evidence applies to the observed KS app build; revalidate after an app update.",
            "Other Portal SKUs and launcher qualification are not covered.",
        ),
        rollback_notes="No new privilege or device setting was added; no rollback required.",
    ),
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
    """The matrix a model must pass on this recipe version to be `supported`.

    Launcher selection is required only where the recipe actually takes over
    the Home launcher; demanding it of Portal TV would make a `supported` state
    unreachable for hardware that cannot do it.
    """
    required = {
        SCENARIO_CLEAN_INSTALL,
        SCENARIO_EXISTING_REUSE,
        SCENARIO_UPDATE,
        SCENARIO_REINSTALL,
        SCENARIO_PERMISSION_CONVERGENCE,
        SCENARIO_HEALTH_VERSION_READBACK,
        SCENARIO_UNINSTALL,
    }
    if recipe.home_launcher_supported:
        required.add(SCENARIO_LAUNCHER_SELECTION)
    return frozenset(required)


def _find_recipe(
    catalog: Catalog, recipe_key: str, version: str
) -> InstallRecipe | None:
    for recipe in catalog.recipes:
        if recipe.recipe_key == recipe_key and recipe.version == version:
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
        recipe = _find_recipe(catalog, assignment.recipe_key, assignment.recipe_version)
        if recipe is None:
            raise CatalogError(
                f"assignment for {assignment.model_key!r} references unknown install "
                f"recipe {assignment.recipe_identity!r}"
            )
        # Checked for every state, not just approved: a proposed assignment of
        # a launcher-capable recipe to launcher-incapable hardware is a source
        # defect now, and discovering it at the moment someone flips it to
        # approved is the worst possible time.
        if recipe.home_launcher_supported and not model.home_launcher_capable:
            raise CatalogError(
                f"{assignment.model_key!r} cannot be assigned {recipe.identity!r}: the "
                f"recipe takes over the home launcher and this hardware is recorded as "
                f"home launcher incapable"
            )
        if assignment.state != ASSIGNMENT_APPROVED:
            continue
        if assignment.model_key in approved_seen:
            raise CatalogError(
                f"device model {assignment.model_key!r} has more than one approved recipe "
                f"assignment; exactly one recipe version may be executable"
            )
        approved_seen.add(assignment.model_key)

    for record in catalog.qualifications:
        if not record.__dataclass_params__.frozen:
            raise CatalogError(f"qualification for {record.model_key} is mutable")
        if _find_model(catalog, record.model_key) is None:
            raise CatalogError(
                f"qualification references unknown device model {record.model_key!r}"
            )
        if _find_recipe(catalog, record.recipe_key, record.recipe_version) is None:
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
    recipe = _find_recipe(catalog, assignment.recipe_key, assignment.recipe_version)
    if recipe is None:  # pragma: no cover -- validate_catalog rejects this
        raise NoApprovedRecipe(
            f"approved assignment {assignment.recipe_identity!r} names a recipe version "
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

    recipe = _find_recipe(catalog, assignment.recipe_key, assignment.recipe_version)
    if recipe is None:  # pragma: no cover -- validate_catalog rejects this
        return (
            SUPPORT_BLOCKED,
            f"approved assignment {assignment.recipe_identity} names a recipe version "
            f"that does not exist",
        )

    for_model = [q for q in catalog.qualifications if q.model_key == model.model_key]
    for_recipe_key = [q for q in for_model if q.recipe_key == assignment.recipe_key]
    if not for_recipe_key:
        return (
            SUPPORT_RECIPE_ASSIGNED,
            f"{model.model_key} is approved for {assignment.recipe_identity} but has no "
            f"qualification evidence of its own on file",
        )

    current = [q for q in for_recipe_key if q.recipe_version == assignment.recipe_version]
    if not current:
        stale = ", ".join(sorted({q.recipe_version for q in for_recipe_key}))
        return (
            SUPPORT_REVALIDATION_REQUIRED,
            f"qualification evidence for {model.model_key} covers {assignment.recipe_key} "
            f"{stale} only; {assignment.recipe_identity} has none",
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
        _find_recipe(catalog, assignment.recipe_key, assignment.recipe_version)
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
