"""Opt-in Device Owner enrollment for Kiosk Satellite (KSM-BEHAVE-088..090, #54).

Android's `dpm set-device-owner` refuses while any account exists. On some
models every account is authenticated by one OEM package, and AccountManager
drops an account the moment its authenticator package is uninstalled for the
user. So enrollment is:

    uninstall -k --user 0 <allowlisted pkg>  ->  accounts gone
    dpm set-device-owner <KS receiver>
    cmd package install-existing --user 0 <pkg>  (always, even on failure)

and success is only ever the `dumpsys device_policy` readback naming Kiosk
Satellite. Only packages listed for the exact device model are ever passed to
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

KS_ADMIN = f"{KS_PACKAGE}/.KioskAdminReceiver"

_POLL_INTERVAL_S = 1.0
_POLL_ATTEMPTS = 15

# KSM-BEHAVE-089: per exact device_models key, the account-registering packages
# KSM may temporarily remove for this user. Each entry needs live evidence that
# removal purges the accounts and install-existing restores the package
# (docs/SPEC/device-management-strategy.md section 6).
ACCOUNT_CLEAR_PACKAGES: dict[str, tuple[str, ...]] = {
    "portal_mini": ("com.facebook.alohaservices.alohausers",),
}

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

    @property
    def ready(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class EnrollResult:
    cleared_packages: tuple[str, ...]


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


def _owner_package(policy: str) -> str | None:
    match = _OWNER_RE.search(policy)
    return match.group(1) if match else None


async def read_owner_package(client: ShellClient) -> str | None:
    """Read-only: the current Device Owner package, or None (KSM-BEHAVE-091, #55).

    One shell call -- the diagnostic sensor's own poll, lighter than the
    four-call run_preflight() this shares its parser with.
    """
    return _owner_package(await client.shell("dumpsys device_policy"))


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
    return EnrollResult(cleared_packages=pre.clear_packages)
