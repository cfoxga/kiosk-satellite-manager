"""Immutable provisioning recipes (KSM-BEHAVE-049/132).

A recipe owns *behavior*: which artifact, which permissions and AppOps, whether
Device Admin is set, where the device
name is read from, and what the authoritative postconditions are. It owns no
hardware matching and no support claim -- those are `device_models.py` and
`device_catalog.py` respectively.

Several exact models may share one recipe.
Sharing a recipe never transfers
qualification or recovery status between those models; see
`device_catalog.derive_support_state` and `oem_recovery.py`.

**A recipe can never carry an arbitrary shell command.** It names audited
operation identifiers from `ALLOWED_OPERATIONS` and typed parameters from
`ALLOWED_PARAMETERS`; `validate_recipe` additionally refuses any string field
carrying shell metacharacters, so a command cannot be smuggled through a field
that legitimately holds a fragment such as `settings get secure bluetooth_name`.
The executor is `install.py`, which switches on those identifiers -- it never
evaluates recipe text as a command line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, fields

# Audited operations. Each maps to a specific, reviewed code path in
# install.py; adding one here is a code change, by design.
OP_PM_INSTALL = "pm_install"
OP_PM_UNINSTALL = "pm_uninstall"
OP_AM_START = "am_start"
OP_GRANT_RUNTIME_PERMISSIONS = "grant_runtime_permissions"
OP_SET_APPOPS = "set_appops"
OP_BATTERY_EXEMPTION = "battery_exemption"
OP_CONVERGE_CONSENT_SERVICES = "converge_consent_services"
OP_SET_DEVICE_ADMIN = "set_device_admin"
OP_SYNC_KS_SETTINGS = "sync_ks_settings"
OP_VERIFY_HEALTH = "verify_health"
OP_DISABLE_PRIVATE_DNS = "disable_private_dns"

ALLOWED_OPERATIONS: frozenset[str] = frozenset(
    {
        OP_PM_INSTALL,
        OP_PM_UNINSTALL,
        OP_AM_START,
        OP_GRANT_RUNTIME_PERMISSIONS,
        OP_SET_APPOPS,
        OP_BATTERY_EXEMPTION,
        OP_CONVERGE_CONSENT_SERVICES,
        OP_SET_DEVICE_ADMIN,
        OP_SYNC_KS_SETTINGS,
        OP_VERIFY_HEALTH,
        OP_DISABLE_PRIVATE_DNS,
    }
)

ALLOWED_PARAMETERS: frozenset[str] = frozenset(
    {
        "browser.ignore_ssl_errors",
        "ha.auto_connect",
    }
)

ARTIFACT_GITHUB_RELEASE_BY_ABI = "github_release_matching_abi"

NAME_SOURCE_SECURE_BLUETOOTH = "secure:bluetooth_name"
NAME_SOURCE_GLOBAL_DEVICE_NAME = "global:device_name"

_NAME_SOURCE_COMMANDS: dict[str, str] = {
    NAME_SOURCE_SECURE_BLUETOOTH: "settings get secure bluetooth_name",
    NAME_SOURCE_GLOBAL_DEVICE_NAME: "settings get global device_name",
}

PERMISSION_POLICY_PORTAL = "portal_sdk_gated"
PERMISSION_POLICY_STANDARD = "standard_sdk_gated"
REPURPOSE_NONE = "none"
REPURPOSE_ANDROID9_META = "android9_meta_confirmed"

INSTALL_PRESERVE_MATCHING_VERSION = "preserve_matching_version"
UPDATE_REINSTALL_ON_VERSION_MISMATCH = "reinstall_on_version_mismatch"
REINSTALL_UNINSTALL_THEN_INSTALL = "uninstall_then_install"
UNINSTALL_PM_UNINSTALL = "pm_uninstall"

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Anything that would let a typed fragment become its own command line.
_SHELL_METACHARACTERS = (";", "&", "|", "$", "`", "\n", "\r", ">", "<", "(", ")", "\\", "'", '"')
_COMMANDISH_FIELD_NAMES = frozenset({"command", "commands", "shell", "script", "cmd", "exec"})

# Every string-bearing field is scanned for shell metacharacters *except*
# these two, which are human prose no code path executes. Stated as an
# exemption list rather than an allowlist of scanned fields on purpose: a
# field added to `InstallRecipe` later is scanned by default, instead of
# silently escaping the check because nobody remembered to enrol it.
_PROSE_FIELDS: frozenset[str] = frozenset({"name", "postconditions"})

# Operation identifier -> the `InstallRecipe` field `install.py` actually
# branches on for it. Kept in sync by `validate_recipe`; see the check there.
_CONDITIONAL_OPERATIONS: dict[str, str] = {
    OP_BATTERY_EXEMPTION: "battery_exemption",
    OP_SET_DEVICE_ADMIN: "sets_device_admin",
    OP_DISABLE_PRIVATE_DNS: "disables_private_dns",
}


class RecipeError(ValueError):
    """A recipe violates the typed-operation/no-shell-text contract."""


@dataclass(frozen=True)
class InstallRecipe:
    """One immutable provisioning behavior; a changed behavior gets a new key."""

    recipe_key: str
    name: str
    artifact_policy: str = ARTIFACT_GITHUB_RELEASE_BY_ABI
    device_name_source: str = NAME_SOURCE_GLOBAL_DEVICE_NAME
    redundant_name_suffixes: tuple[str, ...] = ()
    start_url_path: str = ""
    install_strategy: str = INSTALL_PRESERVE_MATCHING_VERSION
    update_strategy: str = UPDATE_REINSTALL_ON_VERSION_MISMATCH
    reinstall_strategy: str = REINSTALL_UNINSTALL_THEN_INSTALL
    uninstall_behavior: str = UNINSTALL_PM_UNINSTALL
    permission_policy: str = PERMISSION_POLICY_STANDARD
    appops_policy: str = PERMISSION_POLICY_STANDARD
    repurpose_policy: str = REPURPOSE_NONE
    # Conditional behavior. Each is read by `install.py` and is
    # cross-checked against `operations` by `validate_recipe`, so the audited
    # operation list can never drift away from what actually executes.
    battery_exemption: bool = True
    sets_device_admin: bool = False
    # KSM-BEHAVE-152: turn Android's opportunistic Private DNS off, so the
    # device resolves through the network's own DNS. A strict hostname the
    # user chose is never changed.
    disables_private_dns: bool = False
    verifier_retry_on_failure: bool = False
    operations: tuple[str, ...] = ()
    parameters: tuple[tuple[str, str | int | bool], ...] = ()
    postconditions: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        """The stable behavior key that qualification evidence pins."""
        return self.recipe_key

    @property
    def device_name_command(self) -> str:
        """The read-only ADB command for this recipe's declared name source.

        The mapping is closed (`_NAME_SOURCE_COMMANDS`): a recipe names a
        source, it does not supply a command string.
        """
        return _NAME_SOURCE_COMMANDS[self.device_name_source]

    def normalize_device_name(self, value: str) -> str:
        """Remove a model suffix that Android appends to a user-set label."""
        value = (value or "").strip()
        for suffix in self.redundant_name_suffixes:
            if suffix and value.endswith(suffix):
                stripped = value[: -len(suffix)].rstrip()
                if stripped:
                    return stripped
        return value

    def permissions_for_sdk(self, sdk: int) -> list[str]:
        """Runtime permissions adapted to what this API level actually defines.

        Only permissions Kiosk Satellite declares: Android refuses `pm grant`
        for anything else (KSM-BEHAVE-151).
        """
        perms = [
            "android.permission.RECORD_AUDIO",
            "android.permission.CAMERA",
            "android.permission.ACCESS_COARSE_LOCATION",
            "android.permission.ACCESS_FINE_LOCATION",
            "android.permission.READ_LOGS",
        ]
        if sdk >= 31:
            perms.extend(
                ["android.permission.BLUETOOTH_SCAN", "android.permission.BLUETOOTH_CONNECT"]
            )
        if sdk >= 33:
            perms.extend(
                [
                    "android.permission.POST_NOTIFICATIONS",
                    "android.permission.READ_MEDIA_IMAGES",
                    "android.permission.READ_MEDIA_VIDEO",
                ]
            )
        if sdk <= 32:
            perms.append("android.permission.READ_EXTERNAL_STORAGE")
        if sdk <= 29:
            perms.append("android.permission.WRITE_EXTERNAL_STORAGE")
        return perms

    @property
    def replaces_launcher_by_default(self) -> bool:
        return self.permission_policy == PERMISSION_POLICY_PORTAL

    def appops_for_sdk(self, sdk: int) -> list[str]:
        """AppOps grants for this recipe at this API level."""
        ops = ["SYSTEM_ALERT_WINDOW", "WRITE_SETTINGS", "GET_USAGE_STATS"]
        if sdk >= 30:
            ops.append("MANAGE_EXTERNAL_STORAGE")
        return ops


def validate_recipe(recipe: InstallRecipe) -> None:
    """Raise `RecipeError` unless the recipe is typed, audited and shell-free."""
    if not _KEY_RE.match(recipe.recipe_key):
        raise RecipeError(f"recipe_key {recipe.recipe_key!r} is not a stable lower_snake key")

    for name in _COMMANDISH_FIELD_NAMES:
        if name in recipe.__dataclass_fields__:
            raise RecipeError(f"recipe field {name!r} would allow arbitrary shell text")

    unknown_ops = sorted(set(recipe.operations) - ALLOWED_OPERATIONS)
    if unknown_ops:
        raise RecipeError(
            f"{recipe.identity} names undeclared operation(s) {', '.join(unknown_ops)}; "
            f"only audited identifiers in ALLOWED_OPERATIONS may appear"
        )

    # The operation list is the audited surface a reviewer reads; the booleans
    # are what `install.py` branches on. If the two can disagree, the list is
    # decoration and the review it exists to support is worthless -- so a
    # recipe that sets Device Admin without declaring it, or declares it
    # without setting it, is a source defect either way.
    declared_ops = set(recipe.operations)
    for op, flag in _CONDITIONAL_OPERATIONS.items():
        enabled = getattr(recipe, flag)
        if enabled and op not in declared_ops:
            raise RecipeError(
                f"{recipe.identity} has {flag}=True but does not declare operation {op!r}"
            )
        if op in declared_ops and not enabled:
            raise RecipeError(
                f"{recipe.identity} declares operation {op!r} but {flag} is False"
            )

    for f in fields(recipe):
        if f.name in _PROSE_FIELDS:
            continue
        value = getattr(recipe, f.name)
        for text in _iter_strings(value):
            bad = [c for c in _SHELL_METACHARACTERS if c in text]
            if bad:
                raise RecipeError(
                    f"{recipe.identity} field {f.name!r} contains shell "
                    f"metacharacter(s) {bad!r}; recipe data is typed, not executable"
                )
    if recipe.repurpose_policy not in (REPURPOSE_NONE, REPURPOSE_ANDROID9_META):
        raise RecipeError(f"repurpose_policy {recipe.repurpose_policy!r} is not approved")

    for key, value in recipe.parameters:
        if key not in ALLOWED_PARAMETERS:
            raise RecipeError(f"{recipe.identity} sets undeclared parameter {key!r}")
        if not isinstance(value, (str, int, bool)):
            raise RecipeError(f"{recipe.identity} parameter {key!r} is not a typed scalar")

    if recipe.device_name_source not in _NAME_SOURCE_COMMANDS:
        raise RecipeError(
            f"{recipe.identity} device_name_source {recipe.device_name_source!r} is not a "
            f"declared name source"
        )
    if recipe.permission_policy not in (PERMISSION_POLICY_PORTAL, PERMISSION_POLICY_STANDARD):
        raise RecipeError(f"{recipe.identity} permission_policy is not declared")


def _iter_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, tuple):
        for item in value:
            yield from _iter_strings(item)


_PORTAL_REDUNDANT_SUFFIXES: tuple[str, ...] = (
    " Portal",
    " PortalGo",
    " PortalMini",
    " Portal+",
    " PortalPlus",
    " PortalTV",
)

_PORTAL_OPERATIONS: tuple[str, ...] = (
    OP_PM_INSTALL,
    OP_AM_START,
    OP_GRANT_RUNTIME_PERMISSIONS,
    OP_SET_APPOPS,
    OP_BATTERY_EXEMPTION,
    OP_CONVERGE_CONSENT_SERVICES,
    OP_SET_DEVICE_ADMIN,
    OP_DISABLE_PRIVATE_DNS,
    OP_VERIFY_HEALTH,
    OP_SYNC_KS_SETTINGS,
    OP_PM_UNINSTALL,
)

_PORTAL_POSTCONDITIONS: tuple[str, ...] = (
    "pm install reports Success and the installed versionName matches the target release",
    "the device's own /api/health reports the target appVersion",
    "every requested runtime permission and AppOp reads back granted from the device",
    "the battery-optimization exemption reads back applied",
)

# KSM-BEHAVE-152 (#121): this OEM build adds a public resolver to the DHCP DNS
# list, and Android's default opportunistic Private DNS prefers it, so the
# device resolves a split-horizon dashboard host to its public address.
_LOCAL_DNS_POSTCONDITIONS: tuple[str, ...] = _PORTAL_POSTCONDITIONS + (
    "Android Private DNS reads back off, unless the user chose a strict hostname",
)


INSTALL_RECIPES: tuple[InstallRecipe, ...] = (
    InstallRecipe(
        recipe_key="onn_4k_pro_android14",
        name="onn 4K Pro (Android 14)",
        device_name_source=NAME_SOURCE_GLOBAL_DEVICE_NAME,
        permission_policy=PERMISSION_POLICY_STANDARD,
        sets_device_admin=False,
        operations=(
            OP_PM_INSTALL,
            OP_AM_START,
            OP_GRANT_RUNTIME_PERMISSIONS,
            OP_SET_APPOPS,
            OP_BATTERY_EXEMPTION,
            OP_CONVERGE_CONSENT_SERVICES,
            OP_VERIFY_HEALTH,
            OP_SYNC_KS_SETTINGS,
            OP_PM_UNINSTALL,
        ),
        parameters=(("browser.ignore_ssl_errors", True),),
        postconditions=_PORTAL_POSTCONDITIONS,
    ),
    InstallRecipe(
        recipe_key="meta_portal_android10_local_dns",
        name="Meta Portal (Android 10)",
        device_name_source=NAME_SOURCE_SECURE_BLUETOOTH,
        redundant_name_suffixes=_PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        permission_policy=PERMISSION_POLICY_PORTAL,
        sets_device_admin=True,
        disables_private_dns=True,
        verifier_retry_on_failure=True,
        operations=_PORTAL_OPERATIONS,
        parameters=(("browser.ignore_ssl_errors", True),),
        postconditions=_LOCAL_DNS_POSTCONDITIONS,
    ),
    InstallRecipe(
        recipe_key="meta_portal_android9_local_dns",
        name="Meta Portal (Android 9)",
        device_name_source=NAME_SOURCE_SECURE_BLUETOOTH,
        redundant_name_suffixes=_PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        permission_policy=PERMISSION_POLICY_PORTAL,
        repurpose_policy=REPURPOSE_ANDROID9_META,
        sets_device_admin=True,
        disables_private_dns=True,
        verifier_retry_on_failure=True,
        operations=_PORTAL_OPERATIONS,
        parameters=(("browser.ignore_ssl_errors", True),),
        postconditions=_LOCAL_DNS_POSTCONDITIONS,
    ),
    InstallRecipe(
        recipe_key="meta_portal_tv_local_dns",
        name="Meta Portal TV (cleanup unqualified)",
        device_name_source=NAME_SOURCE_SECURE_BLUETOOTH,
        redundant_name_suffixes=_PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        permission_policy=PERMISSION_POLICY_PORTAL,
        sets_device_admin=True,
        disables_private_dns=True,
        verifier_retry_on_failure=True,
        operations=_PORTAL_OPERATIONS,
        parameters=(("browser.ignore_ssl_errors", True),),
        postconditions=_LOCAL_DNS_POSTCONDITIONS,
    ),
    # Generic TV behavior remains unassigned. The live-identified onn 4K Pro
    # Android 14 model uses its own recipe above; the broad `gtv_stick`
    # classification never authorizes provisioning (KSM-BEHAVE-048).
    InstallRecipe(
        recipe_key="android_tv",
        name="Android TV / Google TV stick",
        device_name_source=NAME_SOURCE_GLOBAL_DEVICE_NAME,
        start_url_path="",
        permission_policy=PERMISSION_POLICY_STANDARD,
        sets_device_admin=False,
        operations=(
            OP_PM_INSTALL,
            OP_AM_START,
            OP_GRANT_RUNTIME_PERMISSIONS,
            OP_SET_APPOPS,
            OP_BATTERY_EXEMPTION,
            OP_CONVERGE_CONSENT_SERVICES,
            OP_VERIFY_HEALTH,
            OP_SYNC_KS_SETTINGS,
            OP_PM_UNINSTALL,
        ),
        parameters=(("browser.ignore_ssl_errors", True),),
        postconditions=_PORTAL_POSTCONDITIONS,
    ),
)


def get_recipe(recipe_key: str | None) -> InstallRecipe | None:
    """Look up one exact recipe key, or None -- never a stand-in default."""
    if not recipe_key:
        return None
    for recipe in INSTALL_RECIPES:
        if recipe.recipe_key == recipe_key:
            return recipe
    return None
