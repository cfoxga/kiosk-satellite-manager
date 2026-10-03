"""Opt-in Device Owner enrollment for Kiosk Satellite (KSM-BEHAVE-088..090, #54).

Android's `dpm set-device-owner` refuses while any account exists. On some
models every account is authenticated by one OEM package, and AccountManager
drops an account the moment its authenticator package is uninstalled for the
user. So enrollment is:

    uninstall -k --user 0 <allowlisted pkg>  ->  accounts gone
    dpm set-device-owner <KS receiver>
    cmd package install-existing --user 0 <pkg>  (always, even on failure)

and success is only ever the `dumpsys device_policy` readback naming Kiosk
Satellite. On a Meta Portal the purge also wipes the Meta identity (the
package's credential-encrypted data), so the FB/WhatsApp login has to be set
up again: `restart_meta_setup` resets Meta's self-disabled setup app and puts
its first-run wizard on screen (KSM-BEHAVE-111). Only packages listed for the exact device model are ever passed to
a shell command; an account owned by anything else blocks. Account names are
never parsed into, stored in, or reported from this module.

Nothing here runs automatically -- the device entry's Configure -> Enable
Device Owner step calls it after an explicit confirmation.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Protocol

from .const import KS_PACKAGE
from .install_recipes import InstallRecipe, REPURPOSE_ANDROID9_META

KS_ADMIN = f"{KS_PACKAGE}/.KioskAdminReceiver"

_POLL_INTERVAL_S = 1.0
_POLL_ATTEMPTS = 15

# KSM-BEHAVE-089: per exact device_models key, the account-registering packages
# KSM may temporarily remove for this user. Each entry needs live evidence that
# removal purges the accounts and install-existing restores the package
# (docs/SPEC/device-management-strategy.md section 6).
ACCOUNT_CLEAR_PACKAGES: dict[str, tuple[str, ...]] = {
    "portal_mini": ("com.facebook.alohaservices.alohausers",),
    "portal_go": ("com.facebook.alohaservices.alohausers",),
    "portal_gen2": ("com.facebook.alohaservices.alohausers",),
    # KSM-BEHAVE-168: same shape as Gen 2 (2026-10-03 read-only ADB); Chris
    # approved the first live enrollment as its purge/restore evidence.
    "portal_plus_gen2": ("com.facebook.alohaservices.alohausers",),
    "portal_gen1": ("com.facebook.alohaservices.alohausers",),
}

# KSM-BEHAVE-136: audited Android 9 aloha packages from unmetaportal's package
# set. These are fixed source constants, never device or recipe supplied text.
# The Meta settings, system, input and ADB implementation packages are absent.
ANDROID9_CLEAR_PACKAGES = (
    "com.facebook.alohaservices.alohausers",
    "com.facebook.alohaapps.personaluser",
    "com.facebook.aloha.state",
    "com.facebook.alohaapps.launcher",
    "com.facebook.aloha.app.messenger",
    "com.facebook.aloha.app.whatsapp",
    "com.facebook.alohaapps.contacts",
    "com.facebook.aloha.app.portalfeed",
    "com.facebook.alohaservices.presence",
)
ANDROID9_DISABLE_PACKAGES = (
    "com.facebook.alohaapps.launcher",
    "com.facebook.alohaservices.abilitymanager",
    "com.facebook.alohaservices.alohausers",
    "com.facebook.aloha.app.messenger",
    "com.facebook.aloha.app.whatsapp",
    "com.facebook.aloha.app.portalfeed",
    "com.facebook.aloha.app.storytime",
    "com.facebook.aloha.app.cameraeditor",
    "com.facebook.alohaapps.contacts",
    "com.facebook.alohaapps.personaluser",
    "com.facebook.alohaapps.superframe",
    "com.facebook.alohaservices.presence",
    "com.facebook.alohaservices.abilities.pages",
    "com.facebook.aloha.analytics",
    "com.facebook.aloha.websafety",
    "com.facebook.alohaapps.bugreporter",
)
ANDROID9_REQUIRED_PACKAGES = frozenset({
    "com.facebook.alohaservices.alohausers",
    "com.facebook.alohaapps.launcher",
    "com.facebook.alohaservices.abilitymanager",
})
_ANDROID9_HOME_QUERY = (
    "cmd package resolve-activity --brief -a android.intent.action.MAIN "
    "-c android.intent.category.HOME"
)
_KS_HOME = f"{KS_PACKAGE}/.HomeAlias"

# KSM-BEHAVE-111: Meta's first-run setup app, on the models where it is
# live-verified (Portal Mini, Portal Go, and Portal Gen 2, 2026-09-28; Portal+ Gen 2
# matches Gen 2 read-only, KSM-BEHAVE-168). It disables itself once
# setup is done and the shell may not re-enable it, but a user-0
# `uninstall -k` + `install-existing` returns it enabled with empty data.
META_SETUP_PACKAGE = "com.facebook.alohaapps.devicesetup"
META_SETUP_ACTIVITY = (
    f"{META_SETUP_PACKAGE}/com.facebook.aloha.app.devicesetup.DeviceSetupActivity"
)
META_SETUP_MODELS = frozenset(
    {"portal_mini", "portal_go", "portal_gen2", "portal_plus_gen2"}
)
# The login/owner account types Meta setup creates. The hardware account
# (`com.facebook.aloha.hw`) re-registers by itself, so it proves nothing.
META_IDENTITY_TYPES = (
    "com.facebook.aloha.pl",
    "com.facebook.aloha.privowner",
    "com.facebook.aloha.sso",
)

BLOCKER_ACCOUNTS = "accounts_blocked"
BLOCKER_ALREADY_OWNER = "already_owner"
BLOCKER_OTHER_OWNER = "other_owner"
BLOCKER_MULTIPLE_USERS = "multiple_users"
BLOCKER_KS_MISSING = "ks_missing"
BLOCKER_UNOBSERVED = "unobserved"

_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
_ACCOUNT_ROW_RE = re.compile(r"^\s*Account \{(.*)$", re.MULTILINE)
_ACCOUNT_TYPE_RE = re.compile(r"(?:^|[,\s])type=([^,}\s]+)")
# An account row whose type cannot be read is counted under this key; it has
# no authenticator, so it always blocks (fail closed).
UNREADABLE_ACCOUNT = "(unreadable)"
_AUTHENTICATOR_RE = re.compile(
    r"AuthenticatorDescription \{type=([^}]+)\}, ComponentInfo\{([^/}]+)/"
)
_OWNER_RE = re.compile(r"Device Owner:\s*\n\s*admin=ComponentInfo\{([^/}]+)/")
_OWNER_HEADER_RE = re.compile(r"^\s*Device Owner:", re.MULTILINE)


class ShellClient(Protocol):
    async def shell(self, command: str) -> str: ...


class DeviceOwnerError(Exception):
    """Enrollment refused or failed. `code` is stable; the message carries no
    account identifiers."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Preflight:
    ks_installed: bool
    owner_package: str | None
    user_count: int
    account_counts: dict[str, int] = field(default_factory=dict)
    # account type -> registering package, None when no authenticator matched
    account_owners: dict[str, str | None] = field(default_factory=dict)
    account_packages: set[str] = field(default_factory=set)
    clear_packages: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    # Meta login account types absent on a META_SETUP_MODELS device.
    meta_identity_missing: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class EnrollResult:
    cleared_packages: tuple[str, ...]
    # The Meta identity is gone (the purge wiped it, or it already was) and
    # restart_meta_setup applies to this model.
    meta_setup_needed: bool = False


@dataclass(frozen=True)
class Android9CleanupResult:
    owner_enabled: bool
    accounts_remaining: int
    adb_available: bool
    cleared_packages: tuple[str, ...]
    disabled_packages: tuple[str, ...]


def _account_counts(dump: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rest in _ACCOUNT_ROW_RE.findall(dump):
        # The type is the last type= field; names may contain anything else.
        types = _ACCOUNT_TYPE_RE.findall(rest)
        account_type = types[-1] if types else UNREADABLE_ACCOUNT
        counts[account_type] = counts.get(account_type, 0) + 1
    return counts


def _authenticators(dump: str) -> dict[str, str]:
    return {t.strip(): pkg for t, pkg in _AUTHENTICATOR_RE.findall(dump)}


def _identity_missing(counts: dict[str, int]) -> tuple[str, ...]:
    return tuple(t for t in META_IDENTITY_TYPES if t not in counts)


def _owner_package(policy: str) -> str | None:
    match = _OWNER_RE.search(policy)
    return match.group(1) if match else None


async def run_preflight(client: ShellClient, model_key: str | None) -> Preflight:
    """Read-only eligibility check (KSM-BEHAVE-088). Never mutates."""
    accounts_dump = await client.shell("dumpsys account")
    policy = await client.shell("dumpsys device_policy")
    users = await client.shell("pm list users")
    ks_path = await client.shell(f"pm path {KS_PACKAGE}")

    owner = _owner_package(policy)
    blockers: list[str] = []
    if (
        "Accounts:" not in accounts_dump
        or not policy.strip()
        or "UserInfo{" not in users
        # A Device Owner block we cannot parse is not "no owner".
        or (owner is None and _OWNER_HEADER_RE.search(policy))
    ):
        blockers.append(BLOCKER_UNOBSERVED)

    counts = _account_counts(accounts_dump)
    auth = _authenticators(accounts_dump)
    owners = {t: auth.get(t) for t in counts}
    packages = {p for p in owners.values() if p}
    allowed = set(ACCOUNT_CLEAR_PACKAGES.get(model_key or "", ()))
    if any(p is None or p not in allowed for p in owners.values()):
        blockers.append(BLOCKER_ACCOUNTS)
    clear = tuple(sorted(p for p in packages if p in allowed and _PACKAGE_RE.match(p)))

    if owner == KS_PACKAGE:
        blockers.append(BLOCKER_ALREADY_OWNER)
    elif owner is not None:
        blockers.append(BLOCKER_OTHER_OWNER)

    user_count = users.count("UserInfo{")
    if user_count > 1:
        blockers.append(BLOCKER_MULTIPLE_USERS)

    ks_installed = "package:" in ks_path
    if not ks_installed:
        blockers.append(BLOCKER_KS_MISSING)

    return Preflight(
        ks_installed=ks_installed,
        owner_package=owner,
        user_count=user_count,
        account_counts=counts,
        account_owners=owners,
        account_packages=packages,
        clear_packages=clear,
        blockers=tuple(dict.fromkeys(blockers)),
        meta_identity_missing=(
            _identity_missing(counts)
            if model_key in META_SETUP_MODELS and BLOCKER_UNOBSERVED not in blockers
            else ()
        ),
    )


async def _accounts_cleared(client: ShellClient) -> bool:
    for attempt in range(_POLL_ATTEMPTS):
        dump = await client.shell("dumpsys account")
        if "Accounts:" in dump and not _account_counts(dump):
            return True
        if attempt + 1 < _POLL_ATTEMPTS:
            await asyncio.sleep(_POLL_INTERVAL_S)
    return False


async def _restore(client: ShellClient, packages: list[str]) -> list[str]:
    """Reinstall every attempted package for user 0; return those still missing."""
    missing = []
    for pkg in packages:
        await client.shell(f"cmd package install-existing --user 0 {pkg}")
        listed = await client.shell(f"pm list packages --user 0 {pkg}")
        if f"package:{pkg}" not in listed.split():
            missing.append(pkg)
    return missing


async def enable_device_owner(client: ShellClient, model_key: str | None) -> EnrollResult:
    """Clear allowlisted account packages, set KS as Device Owner, restore.

    Raises DeviceOwnerError with code preflight_blocked, clear_failed,
    accounts_remain, set_owner_failed, readback_failed, or restore_failed.
    """
    pre = await run_preflight(client, model_key)
    if not pre.ready:
        raise DeviceOwnerError("preflight_blocked", ", ".join(pre.blockers))

    attempted: list[str] = []
    failure: BaseException | None = None
    try:
        for pkg in pre.clear_packages:
            attempted.append(pkg)
            out = await client.shell(f"pm uninstall -k --user 0 {pkg}")
            if "Success" not in out:
                raise DeviceOwnerError("clear_failed", f"could not remove {pkg} for user 0")
        if pre.clear_packages and not await _accounts_cleared(client):
            raise DeviceOwnerError("accounts_remain", "accounts still present after clearing")
        out = await client.shell(f"dpm set-device-owner {KS_ADMIN}")
        if "Success" not in out:
            raise DeviceOwnerError("set_owner_failed", "dpm set-device-owner was refused")
        if _owner_package(await client.shell("dumpsys device_policy")) != KS_PACKAGE:
            raise DeviceOwnerError(
                "readback_failed", "device_policy does not list Kiosk Satellite as owner"
            )
    except Exception as err:  # noqa: BLE001 -- restore must run on any failure
        failure = err
    try:
        missing = await _restore(client, attempted)
    except Exception:  # noqa: BLE001 -- transport gone: nothing is verified restored
        missing = list(attempted)

    if missing:
        prior = f" (after {getattr(failure, 'code', type(failure).__name__)})" if failure else ""
        raise DeviceOwnerError(
            "restore_failed",
            f"{', '.join(missing)} was not restored{prior}; "
            f"run: adb shell cmd package install-existing --user 0 {missing[0]}",
        )
    if failure:
        raise failure
    return EnrollResult(
        cleared_packages=pre.clear_packages,
        meta_setup_needed=model_key in META_SETUP_MODELS
        and bool(pre.clear_packages or pre.meta_identity_missing),
    )


async def android9_cleanup_preflight(
    client: ShellClient, model_key: str | None, recipe: InstallRecipe,
    *, ks_home_enabled: bool,
) -> None:
    """Read-only exact-device and connection checks before erasing Meta data."""
    if model_key != "portal_gen1" or recipe.repurpose_policy != REPURPOSE_ANDROID9_META:
        raise DeviceOwnerError("cleanup_unsupported", "this exact Portal has not been qualified")
    if not ks_home_enabled:
        raise DeviceOwnerError("ks_home_required", "enable Home in Kiosk Satellite first")
    if (await client.shell("getprop ro.build.version.sdk")).strip() != "28":
        raise DeviceOwnerError("cleanup_unsupported", "Android 9 was not observed")
    if (await client.shell("getprop ro.product.device")).strip() != "aloha":
        raise DeviceOwnerError("cleanup_unsupported", "Portal Gen 1 was not observed")
    manufacturer = (await client.shell("getprop ro.product.manufacturer")).strip().lower()
    model = (await client.shell("getprop ro.product.model")).strip().lower()
    if manufacturer != "facebook" or model != "portal":
        raise DeviceOwnerError("cleanup_unsupported", "Portal Gen 1 identity did not match")
    pre = await run_preflight(client, model_key)
    blockers = tuple(b for b in pre.blockers if b != BLOCKER_ALREADY_OWNER)
    if blockers:
        raise DeviceOwnerError("preflight_blocked", ", ".join(blockers))
    if (await client.shell("settings get global adb_enabled")).strip() != "1":
        raise DeviceOwnerError("adb_required", "ADB is disabled")
    if (await client.shell("getprop service.adb.tcp.port")).strip() != "5555":
        raise DeviceOwnerError("adb_required", "network ADB is not on port 5555")
    for package in ANDROID9_REQUIRED_PACKAGES:
        if "package:" not in await client.shell(f"pm path {package}"):
            raise DeviceOwnerError("package_missing", f"{package} is missing")
    if _KS_HOME not in await client.shell(_ANDROID9_HOME_QUERY):
        raise DeviceOwnerError(
            "ks_home_required", "select Kiosk Satellite as Android Home first",
        )


async def repurpose_android9_portal(
    client: ShellClient, model_key: str | None, recipe: InstallRecipe,
    *, confirmed: bool, ks_home_enabled: bool,
) -> Android9CleanupResult:
    """Explicitly confirmed Gen 1 cleanup. Every postcondition is read back.

    A failure after enrollment may leave a partially cleaned device. We never
    claim success from command output alone or attempt to recreate Meta login.
    """
    if not confirmed:
        raise DeviceOwnerError("confirmation_required")
    await android9_cleanup_preflight(
        client, model_key, recipe, ks_home_enabled=ks_home_enabled,
    )
    pre = await run_preflight(client, model_key)
    if pre.owner_package != KS_PACKAGE:
        await enable_device_owner(client, model_key)

    cleared: list[str] = []
    disabled: list[str] = []
    for package in ANDROID9_CLEAR_PACKAGES:
        if "package:" not in await client.shell(f"pm path {package}"):
            continue
        if "Success" not in await client.shell(f"pm clear {package}"):
            raise DeviceOwnerError("cleanup_partial", f"could not clear {package}")
        cleared.append(package)
    for package in ANDROID9_DISABLE_PACKAGES:
        if "package:" not in await client.shell(f"pm path {package}"):
            continue
        out = await client.shell(f"pm disable-user --user 0 {package}")
        if "disabled" not in out:
            raise DeviceOwnerError("cleanup_partial", f"could not disable {package}")
        disabled.append(package)

    accounts_dump = await client.shell("dumpsys account")
    if "Accounts:" not in accounts_dump or _account_counts(accounts_dump):
        raise DeviceOwnerError("cleanup_partial", "accounts remain or cannot be read")
    if _owner_package(await client.shell("dumpsys device_policy")) != KS_PACKAGE:
        raise DeviceOwnerError("cleanup_partial", "Kiosk Satellite is not Device Owner")
    listed = set((await client.shell("pm list packages -d")).splitlines())
    if any(f"package:{package}" not in listed for package in disabled):
        raise DeviceOwnerError("cleanup_partial", "disabled packages did not read back")
    home = await client.shell(_ANDROID9_HOME_QUERY)
    if _KS_HOME not in home:
        raise DeviceOwnerError("cleanup_partial", "Kiosk Satellite is not Android Home")
    adb_enabled = (await client.shell("settings get global adb_enabled")).strip() == "1"
    adb_port = (await client.shell("getprop service.adb.tcp.port")).strip() == "5555"
    adb_echo = (await client.shell("echo ksm_adb_ready")).strip() == "ksm_adb_ready"
    if not (adb_enabled and adb_port and adb_echo):
        raise DeviceOwnerError("cleanup_partial", "network ADB did not read back available")
    return Android9CleanupResult(
        owner_enabled=True, accounts_remaining=0, adb_available=True,
        cleared_packages=tuple(cleared), disabled_packages=tuple(disabled),
    )


async def read_meta_identity_missing(client: ShellClient) -> tuple[str, ...] | None:
    """The META_IDENTITY_TYPES not registered now; None when unreadable."""
    dump = await client.shell("dumpsys account")
    if "Accounts:" not in dump:
        return None
    return _identity_missing(_account_counts(dump))


async def _front_activity(client: ShellClient) -> str:
    dump = await client.shell("dumpsys activity activities")
    for line in dump.splitlines():
        if "mResumedActivity" in line or "topResumedActivity" in line:
            return line
    return ""


async def restart_meta_setup(client: ShellClient, model_key: str | None) -> None:
    """KSM-BEHAVE-111: reset Meta's setup app and put its wizard on screen.

    Success is the setup activity actually in front, not am start's output:
    a kiosk lock that keeps Kiosk Satellite pinned is a failure. The caller
    turns the lock off first. Raises DeviceOwnerError with code
    meta_setup_unsupported, meta_setup_failed, or restore_failed.
    """
    if model_key not in META_SETUP_MODELS:
        raise DeviceOwnerError("meta_setup_unsupported", "no known Meta setup app on this model")
    pkg = META_SETUP_PACKAGE
    out = await client.shell(f"pm uninstall -k --user 0 {pkg}")
    if "Success" not in out:
        raise DeviceOwnerError("meta_setup_failed", f"could not reset {pkg}")
    if await _restore(client, [pkg]):
        raise DeviceOwnerError(
            "restore_failed",
            f"{pkg} was not restored; "
            f"run: adb shell cmd package install-existing --user 0 {pkg}",
        )
    enabled = await client.shell(f"pm list packages -e --user 0 {pkg}")
    if f"package:{pkg}" not in enabled.split():
        raise DeviceOwnerError("meta_setup_failed", f"{pkg} is still disabled")
    out = await client.shell(f"am start -n {META_SETUP_ACTIVITY}")
    if "Error" in out:
        raise DeviceOwnerError("meta_setup_failed", "the setup screen could not be started")
    for attempt in range(_POLL_ATTEMPTS):
        if pkg in await _front_activity(client):
            return
        if attempt + 1 < _POLL_ATTEMPTS:
            await asyncio.sleep(_POLL_INTERVAL_S)
    raise DeviceOwnerError(
        "meta_setup_failed",
        "the setup screen did not come to the front (is the kiosk lock on?)",
    )
