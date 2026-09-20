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
    PmInstallFailed,
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


async def test_is_ks_installed_true_when_pm_path_returns_a_path(tmp_path):
    """KSM-TEST-008: `pm path` output is the installation check."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device,
        "shell",
        new=AsyncMock(
            return_value="package:/data/app/~~x/me.jxl.kiosk_satellite-1/base.apk\n"
        ),
    ) as mock_shell:
        assert await client.is_ks_installed() is True
    mock_shell.assert_awaited_once_with("pm path me.jxl.kiosk_satellite")


async def test_is_ks_installed_false_when_pm_path_is_empty(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value="\n")):
        assert await client.is_ks_installed() is False


async def test_uninstall_ks_raises_when_package_survives(tmp_path):
    """KSM-TEST-008: a failed uninstall must not look like a success."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "shell", new=AsyncMock(return_value="package:/data/app/ks/base.apk")
    ):
        with pytest.raises(RuntimeError):
            await client.uninstall_ks()


async def test_uninstall_ks_passes_when_package_is_gone(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "shell", new=AsyncMock(side_effect=["", "", "", "", ""])
    ) as mock_shell:
        await client.uninstall_ks()
    assert [call.args[0] for call in mock_shell.await_args_list] == [
        "dpm remove-active-admin --user 0 me.jxl.kiosk_satellite/.KioskAdminReceiver",
        "pm disable-user --user 0 me.jxl.kiosk_satellite",
        "pm clear me.jxl.kiosk_satellite",
        "pm uninstall me.jxl.kiosk_satellite",
        "pm path me.jxl.kiosk_satellite",
    ]


async def test_install_apk_passes_on_success_output(tmp_path):
    """KSM-BEHAVE-035: `pm install`'s own stdout, not a zero shell exit, is the
    authoritative result -- adb-shell's shell() never raises on a device-side
    package-manager failure."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value="Success\n")) as mock_shell:
        await client.install_apk("/data/local/tmp/ks.apk")
    mock_shell.assert_awaited_once_with("pm install -r -g /data/local/tmp/ks.apk")


@pytest.mark.parametrize(
    "output,expected_category",
    [
        ("Failure [INSTALL_FAILED_OLDER_SDK_VERSION: Failed parse]", "unsupported_sdk"),
        ("Failure [INSTALL_FAILED_CPU_ABI_INCOMPATIBLE]", "unsupported_abi"),
        ("Failure [INSTALL_FAILED_NO_MATCHING_ABIS]", "unsupported_abi"),
        ("Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]", "insufficient_storage"),
        ("Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]", "incompatible_signature"),
        ("Failure [INSTALL_FAILED_SHARED_USER_INCOMPATIBLE]", "incompatible_signature"),
        ("Failure [INSTALL_FAILED_INVALID_APK]", "other"),
        ("some garbage the device printed", "other"),
    ],
)
async def test_install_apk_classifies_pm_failure(tmp_path, output, expected_category):
    """KSM-TEST-030: the four artifact-selection compatibility checks the
    issue asks for (ABI, minimum-SDK, storage, signing certificate) all
    surface as a `pm install` failure code -- classify it instead of
    silently proceeding to `am start` an app that was never installed."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value=output)):
        with pytest.raises(PmInstallFailed) as exc_info:
            await client.install_apk("/data/local/tmp/ks.apk")
    assert exc_info.value.category == expected_category


async def test_install_apk_failure_carries_no_raw_output_beyond_the_code(tmp_path):
    """KSM-TEST-031: the exception message stays a short classified summary,
    not the full raw pm install output verbatim."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    raw = "Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE: not enough room on /data, needed 812934871293 bytes]"
    with patch.object(client._device, "shell", new=AsyncMock(return_value=raw)):
        with pytest.raises(PmInstallFailed) as exc_info:
            await client.install_apk("/data/local/tmp/ks.apk")
    assert exc_info.value.code == "INSTALL_FAILED_INSUFFICIENT_STORAGE"
    assert "812934871293" not in str(exc_info.value)
