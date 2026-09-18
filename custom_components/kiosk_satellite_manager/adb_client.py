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


class AdbAuthPending(Exception):
    """The device hasn't accepted the ADB key yet -- needs the on-device Allow tap."""


class AdbConnectFailed(Exception):
    """A raw connection failure (unreachable, wrong port, refused) -- not solved by tapping Allow."""


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

    async def push(self, local_path: str, remote_path: str) -> None:
        await self._device.push(local_path, remote_path)

    @property
    def available(self) -> bool:
        return self._device.available
