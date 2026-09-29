"""Async ADB client wrapper for Kiosk Satellite Manager.

Wraps adb-shell's AdbDeviceTcpAsync. The happy path (connect with an
already-trusted key, shell, getprop) and the AdbAuthPending/AdbConnectFailed
split below were verified live against a production Kiosk Satellite device
-- see the Verified Findings in docs/SPEC/provisioning.md.

KSM-BEHAVE-005: a brand-new, never-approved key does NOT make adb-shell
raise DeviceAuthError while the on-device "Allow USB debugging?" dialog
sits unanswered -- live-confirmed against the Theater GTV (2026-09-18).
adb-shell's connect() uses auth_timeout_s as the AUTH-phase read timeout
(adb_device_async.py connect(), which sets adb_info.transport_timeout_s =
auth_timeout_s before the AUTH exchange), so an unanswered prompt surfaces
as a plain read timeout after the TCP socket is already open --
TcpTimeoutException/AdbTimeoutError with a message like "Reading from
<host>:<port> timed out (5 seconds)". A genuinely unreachable device fails
before that point (refused/no route), as AdbConnectionError/OSError. So
timeouts that happen post-connect are retryable (AdbAuthPending); only a
raw connection failure is AdbConnectFailed.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import re
import stat
import tempfile
from dataclasses import dataclass

from adb_shell.adb_device_async import AdbDeviceTcpAsync
from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_cryptography import CryptographySigner
from adb_shell.exceptions import (
    AdbConnectionError,
    AdbTimeoutError,
    DeviceAuthError,
    TcpTimeoutException,
)

from .const import ADB_PROBE_TIMEOUT_S, KS_PACKAGE

_VERSION_NAME_RE = re.compile(r"\bversionName=([^\s]+)")
_RUNTIME_PERMISSION_RE = re.compile(r"^\s*(android\.permission\.\S+): granted=(true|false)", re.MULTILINE)
_APPOP_MODE_RE = re.compile(r":\s*(\w+)\s*$")
# KSM-BEHAVE-041: the component name after a BIND_* permission is the
# device's own declared intent-filter target -- read here, never guessed
# (docs/developer/android-support/app-lifecycle.md's explicit caution).
_BOUND_SERVICE_RE = re.compile(r"(\S+)/(\S+) filter \S+ permission (android\.permission\.BIND_\S+)")


class AdbAuthPending(Exception):
    """The device hasn't accepted the ADB key yet -- needs the on-device Allow tap."""


class AdbConnectFailed(Exception):
    """A raw connection failure (unreachable, wrong port, refused) -- not solved by tapping Allow."""


class AdbKeySecurityError(Exception):
    """The local private ADB identity is unsafe to use."""


class PmInstallFailed(Exception):
    """`pm install` reported a `Failure [...]` result.

    adb-shell's shell() never raises on a device-side package-manager
    failure -- `pm install`'s own stdout ("Success" vs "Failure [CODE]") is
    the only authoritative signal (KSM-BEHAVE-035). `code`/`category` are
    kept as sanitized attributes for callers to branch on; the message
    itself never repeats the raw failure detail after the colon (device
    paths, byte counts) that some codes include.
    """

    def __init__(self, code: str | None, category: str) -> None:
        self.code = code
        self.category = category
        super().__init__(f"pm install failed: {code or 'unrecognized failure'} ({category})")


# KSM-BEHAVE-035: maps a `pm install` `Failure [INSTALL_FAILED_*]` code to the
# artifact-selection compatibility category it represents -- ABI, minimum-SDK,
# storage, and signing-certificate/update-compatibility are exactly the four
# checks Android's own package manager already performs and reports on
# install; anything else falls back to "other" rather than guessing.
_PM_INSTALL_FAILURE_CATEGORIES: dict[str, str] = {
    "INSTALL_FAILED_OLDER_SDK_VERSION": "unsupported_sdk",
    "INSTALL_FAILED_CPU_ABI_INCOMPATIBLE": "unsupported_abi",
    "INSTALL_FAILED_NO_MATCHING_ABIS": "unsupported_abi",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "insufficient_storage",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "incompatible_signature",
    "INSTALL_FAILED_SHARED_USER_INCOMPATIBLE": "incompatible_signature",
}
_PM_INSTALL_FAILURE_RE = re.compile(r"Failure\s*\[\s*([A-Z_]+)")


def _classify_pm_install_output(output: str) -> tuple[str | None, str]:
    match = _PM_INSTALL_FAILURE_RE.search(output)
    code = match.group(1) if match else None
    return code, _PM_INSTALL_FAILURE_CATEGORIES.get(code, "other")


class PmUninstallFailed(Exception):
    """`pm uninstall` reported an explicit `Failure [...]` result.

    Same rationale as PmInstallFailed (KSM-BEHAVE-035/042): a Failure code is
    authoritative signal `pm uninstall`'s own stdout provides -- `category`
    distinguishes an OEM/user restriction from a Device Policy Manager block
    so a caller can report *why* a supported removal path came up short,
    instead of a bare "still installed".
    """

    def __init__(self, code: str | None, category: str) -> None:
        self.code = code
        self.category = category
        super().__init__(f"pm uninstall failed: {code or 'unrecognized failure'} ({category})")


class UninstallPolicyBlocked(Exception):
    """KS's Device Admin/Owner policy state did not clear via the one
    KSM-supported ADB path (`dpm remove-active-admin`) -- raised instead of
    proceeding to `pm uninstall` (which would fail anyway against a still-
    privileged package) and instead of attempting any unsupported recovery
    such as a factory reset or `dpm wipe-data`, which stays out of scope
    (docs/SPEC/device-management-strategy.md).
    """

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"could not clear device policy state: {category}")


# KSM-BEHAVE-042: OEM/user-restriction failures are the same shaped
# `Failure [DELETE_FAILED_*]` signal pm install already uses -- categorized,
# not guessed at, from Android's own documented DevicePolicyManager/
# PackageManager uninstall failure constants.
_PM_UNINSTALL_FAILURE_CATEGORIES: dict[str, str] = {
    "DELETE_FAILED_DEVICE_POLICY_MANAGER": "device_policy_blocked",
    "DELETE_FAILED_OWNER_BLOCKED": "device_policy_blocked",
    "DELETE_FAILED_USER_RESTRICTED": "oem_restricted",
    "DELETE_FAILED_ABORTED": "oem_restricted",
}
_PM_UNINSTALL_FAILURE_RE = re.compile(r"Failure\s*\[\s*([A-Z_]+)")


def _classify_pm_uninstall_output(output: str) -> tuple[str | None, str]:
    match = _PM_UNINSTALL_FAILURE_RE.search(output)
    code = match.group(1) if match else None
    return code, _PM_UNINSTALL_FAILURE_CATEGORIES.get(code, "other")


@dataclass
class UninstallResult:
    """KSM-BEHAVE-042: what `uninstall_ks()` actually observed and cleared --
    returned so a caller can record policy state instead of assuming a
    zero-exit removal cleared everything. `was_device_owner` is best-effort:
    True only when `dpm get-device-owner` positively confirmed it, since a
    False here can also mean the device's `dpm` build can't report owner
    status at all (live-confirmed on Portal hardware)."""

    was_active_admin: bool
    was_device_owner: bool
    policy_cleared: bool


_KEY_DIR_MODE = 0o700
_PRIVATE_KEY_MODE = 0o600


def _validate_private_key(dir_fd: int, name: str) -> None:
    """Reject a private key that is not exclusively owned by this process."""
    try:
        key_stat = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(key_stat.st_mode):
        raise AdbKeySecurityError("ADB private key is a symlink")
    if not stat.S_ISREG(key_stat.st_mode):
        raise AdbKeySecurityError("ADB private key is not a regular file")
    if key_stat.st_uid != os.geteuid():
        raise AdbKeySecurityError("ADB private key is not owned by this process identity")
    if stat.S_IMODE(key_stat.st_mode) != _PRIVATE_KEY_MODE:
        raise AdbKeySecurityError("ADB private key has an unsafe mode")


def _repair_existing_private_key_mode(dir_fd: int, name: str) -> None:
    """Apply the one safe metadata-only migration for an owned regular key."""
    try:
        key_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as err:
        raise AdbKeySecurityError("ADB private key is invalid or a symlink") from err
    try:
        key_stat = os.fstat(key_fd)
        if not stat.S_ISREG(key_stat.st_mode):
            raise AdbKeySecurityError("ADB private key is not a regular file")
        if key_stat.st_uid != os.geteuid():
            raise AdbKeySecurityError("ADB private key is not owned by this process identity")
        os.fchmod(key_fd, _PRIVATE_KEY_MODE)
    finally:
        os.close(key_fd)


def _open_private_key_dir(key_dir: str) -> int:
    """Open KSM's key directory without following a final-component symlink."""
    try:
        os.mkdir(key_dir, _KEY_DIR_MODE)
    except FileExistsError:
        pass

    try:
        dir_fd = os.open(key_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as err:
        raise AdbKeySecurityError("ADB key directory is invalid or a symlink") from err

    try:
        dir_stat = os.fstat(dir_fd)
        if dir_stat.st_uid != os.geteuid():
            raise AdbKeySecurityError("ADB key directory is not owned by this process identity")
        os.fchmod(dir_fd, _KEY_DIR_MODE)
        return dir_fd
    except BaseException:
        os.close(dir_fd)
        raise


def ensure_adb_key(key_dir: str) -> str:
    """Generate an ADB keypair under key_dir if one doesn't already exist.

    Returns the private key path. Reused across devices (Architecture:
    "Store the key path on the config entry; reuse across devices" -- reuse
    here means every entry created from a given HA instance shares the same
    key_dir/adbkey, not that keygen runs more than once for it).
    """
    dir_fd = _open_private_key_dir(key_dir)
    lock_fd: int | None = None
    temporary_path: str | None = None
    try:
        lock_fd = os.open(
            "adbkey.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, _PRIVATE_KEY_MODE, dir_fd=dir_fd
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            _validate_private_key(dir_fd, "adbkey")
        except FileNotFoundError:
            temporary_fd, temporary_path = tempfile.mkstemp(prefix=".adbkey-", dir=key_dir)
            try:
                os.fchmod(temporary_fd, _PRIVATE_KEY_MODE)
            finally:
                os.close(temporary_fd)

            keygen(temporary_path)
            temporary_name = os.path.basename(temporary_path)
            _validate_private_key(dir_fd, temporary_name)
            try:
                os.link(temporary_name, "adbkey", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            except FileExistsError:
                _validate_private_key(dir_fd, "adbkey")
            else:
                temporary_public = f"{temporary_path}.pub"
                if os.path.exists(temporary_public):
                    try:
                        os.link(
                            f"{temporary_name}.pub", "adbkey.pub", src_dir_fd=dir_fd, dst_dir_fd=dir_fd
                        )
                    except FileExistsError:
                        pass
        except AdbKeySecurityError:
            _repair_existing_private_key_mode(dir_fd, "adbkey")
        _validate_private_key(dir_fd, "adbkey")
        return os.path.join(key_dir, "adbkey")
    finally:
        if temporary_path is not None:
            for path in (temporary_path, f"{temporary_path}.pub"):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(dir_fd)


async def async_probe_adb_port(host: str, port: int, timeout: float = ADB_PROBE_TIMEOUT_S) -> bool:
    """KSM-BEHAVE-079: whether host:port accepts a TCP connection.

    A bare connect, deliberately not an ADB handshake: it opens no ADB
    session, so polling it can never raise the on-device "Allow USB
    debugging?" prompt. It proves adbd is listening, not that our key is
    authorized.
    """
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


class AdbClient:
    """One ADB connection to one Kiosk Satellite device."""

    def __init__(self, host: str, port: int, key_path: str) -> None:
        self._host = host
        self._port = port
        self._key_path = key_path
        self._signer: CryptographySigner | None = None
        self._device = AdbDeviceTcpAsync(host, port, default_transport_timeout_s=10)

    async def connect(self, auth_timeout_s: float = 5) -> None:
        """Connect and authenticate.

        Raises AdbAuthPending if the on-device Allow tap hasn't happened yet
        -- this includes a plain read timeout during the AUTH exchange, not
        just DeviceAuthError (KSM-BEHAVE-005). Raises AdbConnectFailed for a
        raw connection failure (unreachable, refused, wrong port).
        """
        if self._signer is None:
            # CryptographySigner opens the private key synchronously. Service
            # handlers call connect() on Home Assistant's event loop, so keep
            # that filesystem read in a worker thread.
            self._signer = await asyncio.to_thread(CryptographySigner, self._key_path)
        try:
            await self._device.connect(rsa_keys=[self._signer], auth_timeout_s=auth_timeout_s)
        except (DeviceAuthError, AdbTimeoutError, TcpTimeoutException) as err:
            raise AdbAuthPending(str(err)) from err
        except (AdbConnectionError, OSError) as err:
            raise AdbConnectFailed(str(err)) from err

    async def close(self) -> None:
        await self._device.close()

    async def shell(self, command: str) -> str:
        return await self._device.shell(command)

    async def getprop(self, prop: str) -> str:
        return (await self.shell(f"getprop {prop}")).strip()

    async def is_ks_installed(self) -> bool:
        """KSM-BEHAVE-021: does the device already have Kiosk Satellite?"""
        return bool((await self.shell(f"pm path {KS_PACKAGE}")).strip())

    async def installed_version(self) -> str | None:
        """KSM-BEHAVE-040 (Phase 2, "install and update"): the authoritative
        installed versionName, or None if not installed/unreadable. Same
        `dumpsys package` + regex already used by capability_report.py --
        install_and_launch compares this against the release's tag_name to
        decide whether to preserve a compatible install, and to verify the
        postcondition after a fresh install or update.
        """
        output = await self.shell(f"dumpsys package {KS_PACKAGE}")
        match = _VERSION_NAME_RE.search(output)
        return match.group(1) if match else None

    async def is_active_admin(self) -> bool:
        """KSM-BEHAVE-042 (Phase 4, "uninstall"): whether KS's
        KioskAdminReceiver is currently an active Device Admin, read from
        `dumpsys device_policy`'s own "Enabled Device Admins" listing --
        live-confirmed against the Test Portal. Never inferred from `dpm
        remove-active-admin`'s exit code, which raises SecurityException for
        KS's real (non-testOnly) admin on this hardware regardless of
        whether the admin is later actually cleared (KSM-BEHAVE-022)."""
        output = await self.shell("dumpsys device_policy")
        return f"{KS_PACKAGE}/.KioskAdminReceiver" in output

    async def device_owner_component(self) -> str | None:
        """KSM-BEHAVE-042: `dpm get-device-owner`'s own readback, or None
        when the device reports no owner *or* when its `dpm` build doesn't
        support the subcommand at all -- live-confirmed the latter is the
        case on the Test Portal (SDK 29's `dpm` only implements
        set-active-admin/set-device-owner/set-profile-owner/
        remove-active-admin and rejects `get-device-owner` as an unknown
        command). Best-effort/informational only: a None here can mean
        either "confirmed no owner" or "this device can't say" and callers
        must not treat it as a negative proof, only `is_active_admin()`'s
        listing is a reliable read-only signal on this hardware family."""
        output = (await self.shell("dpm get-device-owner")).strip()
        lowered = output.lower()
        if "no device owner" in lowered or "unknown command" in lowered:
            return None
        return output

    async def uninstall_ks(self) -> UninstallResult:
        """KSM-BEHAVE-022/042: remove Kiosk Satellite, distinguishing an
        ordinary package from one holding active Device Admin (or Device
        Owner, always a superset of active-admin privilege) state, and
        verifying both package and policy state afterward instead of
        trusting shell exit codes.

        An ordinary, never-privileged package skips the admin-removal
        attempt entirely (no `dpm remove-active-admin` call at all) rather
        than issuing a pointless one. Where KS *is* an active admin,
        `dpm remove-active-admin` is still attempted but its failure is
        tolerated -- live-confirmed it raises SecurityException for KS's
        real, non-testOnly admin on Portal hardware, and the already-
        established working path is `pm disable-user` + `pm clear`, which is
        what actually lets the subsequent `pm uninstall` succeed
        (KSM-BEHAVE-022). `pm uninstall`'s own output is checked for an
        explicit `Failure [DELETE_FAILED_*]` code (classified device-policy-
        blocked vs. OEM/user-restricted) before falling back to the `pm
        path` read-back that was already the sole postcondition; after a
        confirmed removal, `is_active_admin()` is re-read as a policy-state
        postcondition and raises UninstallPolicyBlocked if a previously
        privileged package's admin registration somehow survived it.
        """
        was_admin = await self.is_active_admin()
        owner_output = await self.device_owner_component()
        was_owner = owner_output is not None and KS_PACKAGE in owner_output

        if was_admin:
            await self.shell(
                f"dpm remove-active-admin --user 0 {KS_PACKAGE}/.KioskAdminReceiver"
            )

        await self.shell(f"pm disable-user --user 0 {KS_PACKAGE}")
        await self.shell(f"pm clear {KS_PACKAGE}")
        output = await self.shell(f"pm uninstall {KS_PACKAGE}")
        if "Failure" in output:
            code, category = _classify_pm_uninstall_output(output)
            raise PmUninstallFailed(code, category)
        if await self.is_ks_installed():
            raise RuntimeError(f"pm uninstall {KS_PACKAGE} did not remove the package")

        policy_cleared = not await self.is_active_admin()
        if (was_admin or was_owner) and not policy_cleared:
            raise UninstallPolicyBlocked("device_owner" if was_owner else "device_admin")

        return UninstallResult(
            was_active_admin=was_admin, was_device_owner=was_owner, policy_cleared=policy_cleared
        )

    async def install_apk(self, remote_path: str) -> None:
        """`pm install -r -g <remote_path>`, verified against pm's own stdout.

        KSM-BEHAVE-035: a zero shell exit is not evidence -- adb-shell's
        shell() has no concept of the remote command's own exit status, only
        whether the ADB shell channel itself worked. `pm install` reports its
        real result as literal "Success" or "Failure [INSTALL_FAILED_...]"
        text; only that text is authoritative for whether the artifact was
        actually accepted.
        """
        output = await self.shell(f"pm install -r -g {remote_path}")
        if "Success" in output and "Failure" not in output:
            return
        code, category = _classify_pm_install_output(output)
        raise PmInstallFailed(code, category)

    async def push(self, local_path: str, remote_path: str) -> None:
        await self._device.push(local_path, remote_path)

    async def granted_permissions(self) -> set[str]:
        """KSM-BEHAVE-041 (Phase 3, "permission convergence"): the
        authoritative granted-permission set, read from the same
        `dumpsys package` block Android's own Settings UI reads -- `pm
        grant`'s exit code is never evidence a permission actually took."""
        output = await self.shell(f"dumpsys package {KS_PACKAGE}")
        return {
            perm
            for perm, granted in _RUNTIME_PERMISSION_RE.findall(output)
            if granted == "true"
        }

    async def appop_mode(self, op: str) -> str:
        """KSM-BEHAVE-041: `appops set ... allow`'s own shell exit is not
        evidence either -- `cmd appops get` is the readback."""
        output = await self.shell(f"cmd appops get {KS_PACKAGE} {op}")
        match = _APPOP_MODE_RE.search(output.strip())
        return match.group(1) if match else "unknown"

    async def is_battery_exempt(self) -> bool:
        """KSM-BEHAVE-041: readback for `dumpsys deviceidle whitelist
        +<pkg>` -- the mutating and the read-only forms of this command
        differ only by the `+`."""
        output = await self.shell("dumpsys deviceidle whitelist")
        return KS_PACKAGE in output

    async def declared_bound_services(self) -> dict[str, str]:
        """KSM-BEHAVE-041: map each BIND_* permission this package declares
        a service for (e.g. BIND_ACCESSIBILITY_SERVICE) to that service's
        component name, read from the device's own Service Resolver Table.
        Empty if the package declares no such service -- accessibility and
        notification-listener convergence use this to tell "not applicable"
        apart from "needs user interaction" without ever guessing a
        component name."""
        output = await self.shell(f"dumpsys package {KS_PACKAGE}")
        return {
            permission: f"{pkg}/{component}"
            for pkg, component, permission in _BOUND_SERVICE_RE.findall(output)
            if pkg == KS_PACKAGE
        }

    async def get_secure_setting(self, key: str) -> str:
        output = (await self.shell(f"settings get secure {key}")).strip()
        return "" if output == "null" else output

    async def put_secure_setting(self, key: str, value: str) -> None:
        await self.shell(f"settings put secure {key} {value}")

    async def bluetooth_enabled(self) -> bool:
        """KSM-BEHAVE-046 (Phase 5, "functional verification"): the device's
        own Bluetooth radio state, read from `settings get global
        bluetooth_on` -- live-confirmed against the Test Portal ("1" when
        on). This is a device-level fact, not a per-app grant: on SDK < 31
        (the Test Portal is SDK 29) there is no BLUETOOTH_SCAN/CONNECT
        runtime permission at all (`InstallRecipe.permissions_for_sdk`), so
        the radio's own on/off state is the only Bluetooth signal available
        pre-31, and remains the meaningful one post-31 too since a granted
        permission with a disabled radio is not "appropriate behavior" for a
        kiosk that depends on paired Bluetooth peripherals."""
        output = (await self.shell("settings get global bluetooth_on")).strip()
        return output == "1"

    @property
    def available(self) -> bool:
        return self._device.available
