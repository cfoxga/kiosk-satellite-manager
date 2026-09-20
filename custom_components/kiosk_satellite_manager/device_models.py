"""Exact Android device-model identity (KSM-BEHAVE-048, issue #20).

Identity only. This module answers "which exact hardware model/SKU is this?"
and nothing else -- no permissions, no commands, no start URL, no support
claim. Provisioning behavior lives in `install_recipes.py`, the model/recipe
relationship in `device_catalog.py`.

It replaces the combined `DeviceProfile` record, which mixed match rules with
provisioning behavior and so could not represent "Portal Go and Portal Mini are
different hardware that happen to share one install recipe".

Match values are `getprop` output, lower-cased and stripped before comparison
(docs/SPEC/provisioning.md Verified Finding 4). An empty constraint tuple means
"do not constrain on this fact", never "matches anything observed". Facts that
were observed on real hardware but are *not* used to discriminate between
models are recorded in `observed_facts` instead, so the evidence is kept
without widening or narrowing a live-verified match rule.

`meta_portal`, `gtv_stick`, `generic_android` and `unknown` are deliberately
NOT model rows: they are fallback classifications (see
`FALLBACK_CLASSIFICATIONS`), which report what was seen without ever earning an
executable recipe.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

CATALOG_STATE_QUALIFIED_IDENTITY = "qualified_identity"
"""Every match fact in this row was read off live hardware of this exact SKU."""

CATALOG_STATE_PROVISIONAL = "provisional"
"""At least one match fact or SDK bound is inferred, not live-read. Recorded in
`limitations`; such a row still resolves, but its qualification evidence (and
therefore its derived support state) is what decides whether it is supported."""

CLASSIFICATION_META_PORTAL = "meta_portal"
CLASSIFICATION_GTV_STICK = "gtv_stick"
CLASSIFICATION_GENERIC_ANDROID = "generic_android"
CLASSIFICATION_UNKNOWN = "unknown"


@dataclass(frozen=True)
class DeviceFacts:
    """Sanitized, live-observed identity facts for one physical device.

    Every field is `getprop`-shaped and non-identifying: no serial, no IP, no
    account. `sdk` of 0 means "not observed", which is missing evidence and is
    never treated as a satisfied SDK bound.
    """

    manufacturer: str = ""
    brand: str = ""
    model: str = ""
    product: str = ""
    device: str = ""
    board: str = ""
    hardware: str = ""
    characteristics: str = ""
    abi: str = ""
    sdk: int = 0
    fingerprint: str = ""

    @classmethod
    def from_platform(cls, platform: Mapping[str, Any]) -> "DeviceFacts":
        """Build facts from a capability report's `facts.platform` block.

        `codename` is the report's historical name for `ro.product.device`.
        Missing/None values stay empty strings -- absence, not a wildcard.
        """
        sdk = platform.get("sdk")
        return cls(
            manufacturer=platform.get("manufacturer") or "",
            brand=platform.get("brand") or "",
            model=platform.get("model") or "",
            product=platform.get("product") or "",
            device=platform.get("device") or platform.get("codename") or "",
            board=platform.get("board") or "",
            hardware=platform.get("hardware") or "",
            characteristics=platform.get("characteristics") or "",
            abi=platform.get("abi") or "",
            sdk=sdk if isinstance(sdk, int) else 0,
            fingerprint=platform.get("fingerprint") or "",
        )


@dataclass(frozen=True)
class DeviceModel:
    """One exact hardware model/SKU.

    `home_launcher_capable` is a hardware fact (Portal TV has no replaceable
    Home launcher), not a behavior choice -- `device_catalog.validate_catalog`
    refuses to assign a launcher-using recipe to launcher-incapable hardware.
    """

    model_key: str
    name: str
    manufacturer: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    boards: tuple[str, ...] = ()
    hardware: tuple[str, ...] = ()
    characteristics: tuple[str, ...] = ()
    min_sdk: int | None = None
    max_sdk: int | None = None
    match_priority: int = 100
    home_launcher_capable: bool = True
    catalog_state: str = CATALOG_STATE_PROVISIONAL
    observed_facts: tuple[tuple[str, str], ...] = ()
    evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def matches(self, facts: DeviceFacts) -> bool:
        """True when every declared constraint is satisfied by observed facts."""
        constraints = (
            (self.manufacturer, facts.manufacturer),
            (self.brands, facts.brand),
            (self.models, facts.model),
            (self.products, facts.product),
            (self.devices, facts.device),
            (self.boards, facts.board),
            (self.hardware, facts.hardware),
            (self.characteristics, facts.characteristics),
        )
        for allowed, observed in constraints:
            if allowed and _norm(observed) not in allowed:
                return False
        if self.min_sdk is not None and (facts.sdk <= 0 or facts.sdk < self.min_sdk):
            return False
        if self.max_sdk is not None and (facts.sdk <= 0 or facts.sdk > self.max_sdk):
            return False
        return bool(
            self.manufacturer
            or self.brands
            or self.models
            or self.products
            or self.devices
            or self.boards
            or self.hardware
            or self.characteristics
        )


@dataclass(frozen=True)
class FallbackClassification:
    """A "we can say roughly what this is" answer. Never a supported model.

    A classification exists so an operator sees *something* useful about an
    unrecognized device; `device_catalog` never resolves an executable recipe
    from one (KSM-BEHAVE-048).
    """

    key: str
    name: str
    manufacturer: tuple[str, ...] = ()
    characteristics: tuple[str, ...] = ()
    reason: str = ""


def _norm(value: str) -> str:
    return (value or "").strip().lower()


_PORTAL_MATCH_NOTE = (
    "Portal manufacturer/model strings are lower-cased `ro.product.manufacturer`/"
    "`ro.product.model`; `brand: \"Facebook\", model: \"Facebook PortalGo\"` was "
    "live-read via /api/health on the Test Portal (provisioning.md Finding 4)."
)

_PORTAL_GEN_SDK_FLOOR_LIMITATION = (
    "min_sdk is an inferred floor (earliest plausible Portal Android build), not "
    "a live-read boundary. It exists so an unobserved or nonsensical SDK cannot "
    "fall into a generation window; the max_sdk/min_sdk split at 28/29 is the "
    "live-relevant boundary."
)

DEVICE_MODELS: tuple[DeviceModel, ...] = (
    DeviceModel(
        model_key="portal_go",
        name="Meta Portal Go",
        manufacturer=("facebook",),
        models=("portalgo",),
        match_priority=10,
        home_launcher_capable=True,
        catalog_state=CATALOG_STATE_QUALIFIED_IDENTITY,
        observed_facts=(
            ("ro.product.device", "terry"),
            ("ro.build.version.sdk", "29"),
            ("ro.build.characteristics", "nosdcard"),
        ),
        evidence_refs=(
            "tests/fixtures/profile-portal-go-2026-09-20.json",
            "docs/SPEC/provisioning.md KSM-BEHAVE-045",
        ),
        limitations=(
            "Identity only. The fixture is a read-only profile-match "
            "qualification; it certifies no install-lifecycle behavior.",
        ),
    ),
    DeviceModel(
        model_key="portal_mini",
        name="Meta Portal Mini",
        manufacturer=("facebook",),
        models=("portalmini",),
        match_priority=10,
        home_launcher_capable=True,
        catalog_state=CATALOG_STATE_QUALIFIED_IDENTITY,
        observed_facts=(("ro.build.version.sdk", "29"),),
        evidence_refs=("docs/SPEC/oem-recovery.md",),
        limitations=(
            "The live evidence on this hardware is Test Harness recovery "
            "behavior, not install-lifecycle behavior (KSM-BEHAVE-052).",
        ),
    ),
    DeviceModel(
        model_key="portal_gen2",
        name="Meta Portal (Gen 2)",
        manufacturer=("facebook",),
        models=("portal",),
        min_sdk=29,
        match_priority=20,
        home_launcher_capable=True,
        evidence_refs=("docs/developer/android-support/device-playbooks.md",),
        limitations=(_PORTAL_MATCH_NOTE,),
    ),
    DeviceModel(
        model_key="portal_gen1",
        name="Meta Portal (Gen 1)",
        manufacturer=("facebook",),
        models=("portal",),
        min_sdk=24,
        max_sdk=28,
        match_priority=20,
        home_launcher_capable=True,
        evidence_refs=("docs/developer/android-support/device-playbooks.md",),
        limitations=(_PORTAL_MATCH_NOTE, _PORTAL_GEN_SDK_FLOOR_LIMITATION),
    ),
    DeviceModel(
        model_key="portal_plus_gen2",
        name="Meta Portal+ (Gen 2)",
        manufacturer=("facebook",),
        models=("portal+", "portalplus"),
        min_sdk=29,
        match_priority=20,
        home_launcher_capable=True,
        evidence_refs=("docs/developer/android-support/device-playbooks.md",),
        limitations=(_PORTAL_MATCH_NOTE,),
    ),
    DeviceModel(
        model_key="portal_plus_gen1",
        name="Meta Portal+ (Gen 1)",
        manufacturer=("facebook",),
        models=("portal+", "portalplus"),
        min_sdk=24,
        max_sdk=28,
        match_priority=20,
        home_launcher_capable=True,
        evidence_refs=("docs/developer/android-support/device-playbooks.md",),
        limitations=(_PORTAL_MATCH_NOTE, _PORTAL_GEN_SDK_FLOOR_LIMITATION),
    ),
    DeviceModel(
        model_key="portal_tv",
        name="Meta Portal TV",
        manufacturer=("facebook",),
        models=("portaltv",),
        match_priority=10,
        # Behaviorally significant: this SKU has no replaceable Home launcher,
        # which is why it cannot share the standard Portal recipe (#20).
        home_launcher_capable=False,
        evidence_refs=("docs/developer/android-support/device-playbooks.md",),
        limitations=(_PORTAL_MATCH_NOTE,),
    ),
)

FALLBACK_CLASSIFICATIONS: tuple[FallbackClassification, ...] = (
    FallbackClassification(
        key=CLASSIFICATION_META_PORTAL,
        name="Meta Portal (unrecognized model)",
        manufacturer=("facebook", "meta"),
        reason=(
            "A Meta device whose ro.product.model did not match any exact "
            "catalog model. Reported, never provisioned: the pre-catalog code "
            "gave this case the full Portal permission/device-admin recipe on "
            "no evidence at all (KSM-BEHAVE-048)."
        ),
    ),
    FallbackClassification(
        key=CLASSIFICATION_GTV_STICK,
        name="Google TV stick (onn/Chromecast-class, unrecognized model)",
        manufacturer=("onn", "google"),
        characteristics=("tv,nosdcard", "tv"),
        reason=(
            "ro.build.characteristics and ro.product.manufacturer are "
            "live-confirmed for onn 4K/4K Pro (provisioning.md Finding 4), but "
            "no exact ro.product.model has been read off one, so there is no "
            "model row to assign `android_tv:v1` to yet "
            "(cfoxga/kiosk-satellite-manager#18)."
        ),
    ),
    FallbackClassification(
        key=CLASSIFICATION_GENERIC_ANDROID,
        name="Android device (unrecognized model)",
        reason="Identity facts were observed but match no catalog model.",
    ),
)

UNKNOWN_CLASSIFICATION = FallbackClassification(
    key=CLASSIFICATION_UNKNOWN,
    name="Unknown device",
    reason="No usable identity facts were observed.",
)


def get_device_model(model_key: str | None) -> DeviceModel | None:
    """Look up an exact model row, or None -- never a stand-in default."""
    if not model_key:
        return None
    for model in DEVICE_MODELS:
        if model.model_key == model_key:
            return model
    return None


def match_device_model(
    facts: DeviceFacts, models: tuple[DeviceModel, ...] = DEVICE_MODELS
) -> DeviceModel | None:
    """Resolve the exact model, most specific first, or None.

    None means "no exact model", which is missing evidence. It is never
    upgraded into a default model by this module or any caller
    (KSM-BEHAVE-048).
    """
    for model in sorted(models, key=lambda m: m.match_priority):
        if model.matches(facts):
            return model
    return None


def classify_fallback(facts: DeviceFacts) -> FallbackClassification:
    """Describe an unrecognized device without promoting it to supported."""
    for classification in FALLBACK_CLASSIFICATIONS:
        if classification.manufacturer and _norm(facts.manufacturer) not in classification.manufacturer:
            continue
        if classification.characteristics and _norm(facts.characteristics) not in classification.characteristics:
            continue
        if not classification.manufacturer and not classification.characteristics:
            # generic_android: any observed identity fact at all.
            if not any(
                (facts.manufacturer, facts.brand, facts.model, facts.product, facts.device)
            ):
                continue
        return classification
    return UNKNOWN_CLASSIFICATION
