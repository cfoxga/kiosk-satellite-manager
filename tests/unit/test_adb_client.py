"""Unit tests for the ADB client wrapper (KSM-BEHAVE-001/005) -- no hass fixture.

connect()/shell()/getprop() are thin passthroughs to adb-shell's
AdbDeviceTcpAsync, mocked here for the exception-mapping paths;
ensure_adb_key() is exercised for real (keygen is deterministic, offline,
and fast). The success path for connect/shell/getprop was verified live
against a production device -- see docs/SPEC/provisioning.md.

KSM-BEHAVE-005: live-tested against the Theater GTV with a fresh,
never-approved key (2026-09-18) -- the device does NOT raise
DeviceAuthError while the on-device "Allow USB debugging?" dialog is
unanswered. adb-shell's own connect() uses auth_timeout_s as the AUTH-phase
read timeout (adb_device_async.py:306), so an unanswered prompt surfaces as
a plain read timeout (TcpTimeoutException/AdbTimeoutError) after the TCP
socket is already open -- observed live as "Reading from <host>:<port>
timed out (5 seconds)". A raw connect failure (host down, port closed,
refused) fails before that point, as AdbConnectionError/OSError. So the
post-connect timeout classes now map to AdbAuthPending (retryable -- give
the user time to tap Allow), not AdbConnectFailed.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from adb_shell.exceptions import AdbConnectionError, AdbTimeoutError, DeviceAuthError, TcpTimeoutException

from custom_components.kiosk_satellite_manager.adb_client import (
    AdbAuthPending,
    AdbClient,
    AdbConnectFailed,
    ensure_adb_key,
)


def test_ensure_adb_key_generates_once(tmp_path):
    key_dir = str(tmp_path / "keys")
    priv_path = ensure_adb_key(key_dir)
    assert priv_path.endswith("adbkey")
    assert os.path.exists(priv_path)
    assert os.path.exists(priv_path + ".pub")


def test_ensure_adb_key_reuses_existing(tmp_path):
    key_dir = str(tmp_path / "keys")
    first = ensure_adb_key(key_dir)
    with open(first) as fh:
        original = fh.read()
    second = ensure_adb_key(key_dir)
    with open(second) as fh:
        again = fh.read()
    assert first == second
    assert original == again


async def test_connect_raises_auth_pending_on_device_auth_error(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "connect", new=AsyncMock(side_effect=DeviceAuthError("nope"))
    ):
        with pytest.raises(AdbAuthPending):
            await client.connect()


async def test_connect_raises_auth_pending_on_tcp_timeout(tmp_path):
    """KSM-BEHAVE-005: live-observed shape of an unanswered "Allow" prompt."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "connect", new=AsyncMock(side_effect=TcpTimeoutException("timed out"))
    ):
        with pytest.raises(AdbAuthPending):
            await client.connect()


async def test_connect_raises_auth_pending_on_adb_timeout_error(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "connect", new=AsyncMock(side_effect=AdbTimeoutError("timed out"))
    ):
        with pytest.raises(AdbAuthPending):
            await client.connect()


async def test_connect_raises_connect_failed_on_connection_error(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "connect", new=AsyncMock(side_effect=AdbConnectionError("refused"))
    ):
        with pytest.raises(AdbConnectFailed):
            await client.connect()


async def test_connect_raises_connect_failed_on_os_error(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "connect", new=AsyncMock(side_effect=OSError("no route to host"))
    ):
        with pytest.raises(AdbConnectFailed):
            await client.connect()


async def test_getprop_strips_trailing_newline(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value="tv,nosdcard\n")):
        assert await client.getprop("ro.build.characteristics") == "tv,nosdcard"
