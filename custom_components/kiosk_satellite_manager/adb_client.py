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

import os
import re
from pathlib import Path

from adb_shell.adb_device_async import AdbDeviceTcpAsync
from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_cryptography import CryptographySigner
from adb_shell.exceptions import (
    AdbConnectionError,
    AdbTimeoutError,
    DeviceAuthError,
    TcpTimeoutException,
)

from .const import KS_PACKAGE

_VERSION_NAME_RE = re.compile(r"\bversionName=([^\s]+)")


class AdbAuthPending(Exception):
    """The device hasn't accepted the ADB key yet -- needs the on-device Allow tap."""


class AdbConnectFailed(Exception):
    """A raw connection failure (unreachable, wrong port, refused) -- not solved by tapping Allow."""


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


def ensure_adb_key(key_dir: str) -> str:
    """Generate an ADB keypair under key_dir if one doesn't already exist.

    Returns the private key path. Reused across devices (Architecture:
    "Store the key path on the config entry; reuse across devices" -- reuse
    here means every entry created from a given HA instance shares the same
    key_dir/adbkey, not that keygen runs more than once for it).
    """
    Path(key_dir).mkdir(parents=True, exist_ok=True)
    priv_path = os.path.join(key_dir, "adbkey")
    if not os.path.exists(priv_path):
        keygen(priv_path)
    return priv_path


class AdbClient:
    """One ADB connection to one Kiosk Satellite device."""

    def __init__(self, host: str, port: int, key_path: str) -> None:
        self._host = host
        self._port = port
        self._signer = CryptographySigner(key_path)
        self._device = AdbDeviceTcpAsync(host, port, default_transport_timeout_s=10)

    async def connect(self, auth_timeout_s: float = 5) -> None:
        """Connect and authenticate.

        Raises AdbAuthPending if the on-device Allow tap hasn't happened yet
        -- this includes a plain read timeout during the AUTH exchange, not
        just DeviceAuthError (KSM-BEHAVE-005). Raises AdbConnectFailed for a
        raw connection failure (unreachable, refused, wrong port).
        """
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
        """KSM-BEHAVE-039 (Phase 2, "install and update"): the authoritative
        installed versionName, or None if not installed/unreadable. Same
        `dumpsys package` + regex already used by capability_report.py --
        install_and_launch compares this against the release's tag_name to
        decide whether to preserve a compatible install, and to verify the
        postcondition after a fresh install or update.
        """
        output = await self.shell(f"dumpsys package {KS_PACKAGE}")
        match = _VERSION_NAME_RE.search(output)
        return match.group(1) if match else None

    async def uninstall_ks(self) -> None:
        """KSM-BEHAVE-022: remove Kiosk Satellite, verifying it actually went.

        Remove the active admin first because install configures it for Portal
        and unknown profiles. On devices where KioskAdminReceiver is set (e.g.
        Portal), ``dpm remove-active-admin`` fails for non-testOnly apps with
        SecurityException: Attempt to remove non-test admin, which then makes a
        direct ``pm uninstall`` fail with DELETE_FAILED_DEVICE_POLICY_MANAGER.
        Disabling the package for user 0 deactivates the admin components, and
        ``pm clear`` cleans state, allowing ``pm uninstall`` to succeed
        cleanly. Then read back with ``pm path`` and raise rather than
        returning a silently-still-installed device.
        """
        await self.shell(
            f"dpm remove-active-admin --user 0 {KS_PACKAGE}/.KioskAdminReceiver"
        )
        await self.shell(f"pm disable-user --user 0 {KS_PACKAGE}")
        await self.shell(f"pm clear {KS_PACKAGE}")
        await self.shell(f"pm uninstall {KS_PACKAGE}")
        if await self.is_ks_installed():
            raise RuntimeError(f"pm uninstall {KS_PACKAGE} did not remove the package")

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

    @property
    def available(self) -> bool:
        return self._device.available
