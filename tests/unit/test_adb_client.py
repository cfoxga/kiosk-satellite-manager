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
import stat
import threading
from unittest.mock import AsyncMock, patch

import pytest
from adb_shell.exceptions import AdbConnectionError, AdbTimeoutError, DeviceAuthError, TcpTimeoutException

from custom_components.kiosk_satellite_manager.adb_client import (
    AdbAuthPending,
    AdbClient,
    AdbConnectFailed,
    AdbKeySecurityError,
    PmInstallFailed,
    PmUninstallFailed,
    UninstallPolicyBlocked,
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


def test_ensure_adb_key_creates_private_identity_with_private_permissions(tmp_path):
    """[KSM-TEST-104] A permissive process umask must not expose the key."""
    key_dir = tmp_path / "keys"
    previous_umask = os.umask(0o022)
    try:
        private_key = ensure_adb_key(str(key_dir))
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(os.stat(private_key).st_mode) == 0o600


def test_ensure_adb_key_repairs_owned_existing_private_key_mode(tmp_path):
    """[KSM-TEST-105] Existing keys get a metadata-only private-mode migration."""
    private_key = ensure_adb_key(str(tmp_path / "keys"))
    os.chmod(private_key, 0o644)

    assert ensure_adb_key(str(tmp_path / "keys")) == private_key
    assert stat.S_IMODE(os.stat(private_key).st_mode) == 0o600


def test_ensure_adb_key_rejects_symlink_substitution(tmp_path):
    """[KSM-TEST-105] A symlink must never be handed to the signer."""
    key_dir = tmp_path / "keys"
    private_key = ensure_adb_key(str(key_dir))
    os.unlink(private_key)
    os.symlink(tmp_path / "outside", private_key)

    with pytest.raises(AdbKeySecurityError, match="symlink"):
        ensure_adb_key(str(key_dir))


def test_ensure_adb_key_rejects_symlinked_key_directory(tmp_path):
    """[KSM-TEST-105] The managed directory itself cannot redirect key creation."""
    os.symlink(tmp_path / "outside", tmp_path / "keys")

    with pytest.raises(AdbKeySecurityError, match="symlink"):
        ensure_adb_key(str(tmp_path / "keys"))


def test_ensure_adb_key_serializes_concurrent_first_generation(tmp_path):
    """[KSM-TEST-106] One first-run caller publishes the shared identity."""
    key_dir = str(tmp_path / "keys")
    calls = 0
    calls_lock = threading.Lock()

    def fake_keygen(path):
        nonlocal calls
        with calls_lock:
            calls += 1
        with open(path, "wb") as private_key:
            private_key.write(b"private")
        with open(path + ".pub", "wb") as public_key:
            public_key.write(b"public")

    with patch("custom_components.kiosk_satellite_manager.adb_client.keygen", side_effect=fake_keygen):
        threads = [threading.Thread(target=ensure_adb_key, args=(key_dir,)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert calls == 1
    assert (tmp_path / "keys" / "adbkey").read_bytes() == b"private"


def test_ensure_adb_key_rejects_insecure_keygen_output(tmp_path):
    """[KSM-TEST-107] Dependency output is checked before publication."""
    def insecure_keygen(path):
        with open(path, "wb") as private_key:
            private_key.write(b"private")
        os.chmod(path, 0o644)

    with patch(
        "custom_components.kiosk_satellite_manager.adb_client.keygen", side_effect=insecure_keygen
    ), pytest.raises(AdbKeySecurityError, match="mode"):
        ensure_adb_key(str(tmp_path / "keys"))

    assert not (tmp_path / "keys" / "adbkey").exists()


async def test_connect_loads_signer_off_the_event_loop(tmp_path):
    """The signer opens the private key, so construction must not do it inline."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    signer = object()
    with patch(
        "custom_components.kiosk_satellite_manager.adb_client.CryptographySigner"
    ) as signer_cls, patch(
        "custom_components.kiosk_satellite_manager.adb_client.asyncio.to_thread",
        new=AsyncMock(return_value=signer),
    ) as to_thread:
        client = AdbClient("1.2.3.4", 5555, key_path)

        assert client._signer is None
        signer_cls.assert_not_called()
        with patch.object(client._device, "connect", new=AsyncMock()):
            await client.connect()

    to_thread.assert_awaited_once_with(signer_cls, key_path)


async def test_connect_reuses_the_existing_signer_and_delegates_close(tmp_path):
    """[KSM-TEST-116] Reconnects keep one signer and close the ADB transport."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    signer = object()
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch(
        "custom_components.kiosk_satellite_manager.adb_client.CryptographySigner"
    ) as signer_cls, patch(
        "custom_components.kiosk_satellite_manager.adb_client.asyncio.to_thread",
        new=AsyncMock(return_value=signer),
    ) as to_thread, patch.object(client._device, "connect", new=AsyncMock()) as connect, patch.object(
        client._device, "close", new=AsyncMock()
    ) as close:
        await client.connect()
        await client.connect()
        await client.close()

    to_thread.assert_awaited_once_with(signer_cls, key_path)
    assert connect.await_count == 2
    close.assert_awaited_once()


async def test_push_and_bluetooth_readback_use_the_device_transport(tmp_path):
    """[KSM-TEST-117] Bluetooth uses its documented command and exact on value."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "push", new=AsyncMock()) as push, patch.object(
        client._device, "shell", new=AsyncMock(side_effect=[" 1\n", "0\n", "unexpected\n"])
    ) as shell:
        await client.push("/tmp/ks.apk", "/data/local/tmp/ks.apk")
        assert await client.bluetooth_enabled() is True
        assert await client.bluetooth_enabled() is False
        assert await client.bluetooth_enabled() is False

    push.assert_awaited_once_with("/tmp/ks.apk", "/data/local/tmp/ks.apk")
    assert [item.args[0] for item in shell.await_args_list] == [
        "settings get global bluetooth_on",
        "settings get global bluetooth_on",
        "settings get global bluetooth_on",
    ]


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


# KSM-BEHAVE-042: live-captured against the Test Portal -- this device's `dpm`
# build only implements set-active-admin/set-device-owner/set-profile-owner/
# remove-active-admin and rejects `get-device-owner` outright, unlike stock
# AOSP. device_owner_component() must treat this the same as "no owner", not
# crash on it or misread the usage text as a component name.
_LIVE_DPM_GET_DEVICE_OWNER_UNSUPPORTED = (
    "usage: dpm [subcommand] [options]\n"
    "usage: dpm set-active-admin [ --user <USER_ID> | current ] <COMPONENT>\n"
    "\n\nError: unknown command 'get-device-owner'"
)
_LIVE_DEVICE_POLICY_ADMIN_BLOCK = (
    "Current Device Policy Manager state:\n\n"
    "  Enabled Device Admins (User 0, provisioningState: 0):\n"
    "    me.jxl.kiosk_satellite/.KioskAdminReceiver:\n"
    "      uid=10133\n"
    "      testOnlyAdmin=false\n"
    "      policies:\n"
    "        force-lock\n"
)
_NO_ADMINS_BLOCK = "Current Device Policy Manager state:\n\n  Enabled Device Admins (User 0):\n"


async def test_uninstall_ks_ordinary_package_skips_admin_removal_call(tmp_path):
    """KSM-BEHAVE-042: no active admin, no owner -- never issues the
    dpm remove-active-admin call at all, distinguishing an ordinary package
    from a privileged one operationally, not just by label."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device,
        "shell",
        new=AsyncMock(
            side_effect=[
                _NO_ADMINS_BLOCK,  # is_active_admin() pre-check
                "No device owner",  # device_owner_component()
                "",  # pm disable-user
                "",  # pm clear
                "",  # pm uninstall
                "",  # pm path (is_ks_installed -> False)
                _NO_ADMINS_BLOCK,  # is_active_admin() postcondition
            ]
        ),
    ) as mock_shell:
        result = await client.uninstall_ks()
    calls = [call.args[0] for call in mock_shell.await_args_list]
    assert "dpm remove-active-admin --user 0 me.jxl.kiosk_satellite/.KioskAdminReceiver" not in calls
    assert calls == [
        "dumpsys device_policy",
        "dpm get-device-owner",
        "pm disable-user --user 0 me.jxl.kiosk_satellite",
        "pm clear me.jxl.kiosk_satellite",
        "pm uninstall me.jxl.kiosk_satellite",
        "pm path me.jxl.kiosk_satellite",
        "dumpsys device_policy",
    ]
    assert result.was_active_admin is False
    assert result.was_device_owner is False
    assert result.policy_cleared is True


async def test_uninstall_ks_active_admin_attempts_removal_and_verifies_cleared(tmp_path):
    """KSM-BEHAVE-042: an active Device Admin gets the removal attempt, and
    the postcondition (not just `pm path`) is re-read afterward."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device,
        "shell",
        new=AsyncMock(
            side_effect=[
                _LIVE_DEVICE_POLICY_ADMIN_BLOCK,  # is_active_admin() pre-check -> True
                "No device owner",  # device_owner_component() -> not owner
                "",  # dpm remove-active-admin (SecurityException tolerated in practice)
                "",  # pm disable-user
                "",  # pm clear
                "",  # pm uninstall
                "",  # pm path (is_ks_installed -> False)
                _NO_ADMINS_BLOCK,  # is_active_admin() postcondition -> cleared
            ]
        ),
    ) as mock_shell:
        result = await client.uninstall_ks()
    calls = [call.args[0] for call in mock_shell.await_args_list]
    assert calls[2] == "dpm remove-active-admin --user 0 me.jxl.kiosk_satellite/.KioskAdminReceiver"
    assert result.was_active_admin is True
    assert result.was_device_owner is False
    assert result.policy_cleared is True


async def test_uninstall_ks_raises_policy_blocked_when_admin_registration_survives(tmp_path):
    """KSM-TEST-045 negative control: package gone, but the admin
    registration is still listed afterward -- must not report success."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device,
        "shell",
        new=AsyncMock(
            side_effect=[
                _LIVE_DEVICE_POLICY_ADMIN_BLOCK,  # pre-check -> True
                "No device owner",
                "",  # dpm remove-active-admin
                "",  # pm disable-user
                "",  # pm clear
                "",  # pm uninstall
                "",  # pm path -> package gone
                _LIVE_DEVICE_POLICY_ADMIN_BLOCK,  # postcondition still shows the admin
            ]
        ),
    ):
        with pytest.raises(UninstallPolicyBlocked) as excinfo:
            await client.uninstall_ks()
    assert excinfo.value.category == "device_admin"


async def test_uninstall_ks_raises_policy_blocked_with_device_owner_category(tmp_path):
    """KSM-BEHAVE-042: when `dpm get-device-owner` does positively confirm
    KS as owner, an uncleared postcondition is reported as device_owner, not
    the generic device_admin category."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device,
        "shell",
        new=AsyncMock(
            side_effect=[
                _LIVE_DEVICE_POLICY_ADMIN_BLOCK,  # pre-check -> True
                "Device owner: me.jxl.kiosk_satellite/.KioskAdminReceiver",
                "",  # dpm remove-active-admin
                "",  # pm disable-user
                "",  # pm clear
                "",  # pm uninstall
                "",  # pm path -> package gone
                _LIVE_DEVICE_POLICY_ADMIN_BLOCK,  # postcondition still shows the admin
            ]
        ),
    ):
        with pytest.raises(UninstallPolicyBlocked) as excinfo:
            await client.uninstall_ks()
    assert excinfo.value.category == "device_owner"


@pytest.mark.parametrize(
    "output,expected_code,expected_category",
    [
        ("Failure [DELETE_FAILED_DEVICE_POLICY_MANAGER]", "DELETE_FAILED_DEVICE_POLICY_MANAGER", "device_policy_blocked"),
        ("Failure [DELETE_FAILED_OWNER_BLOCKED]", "DELETE_FAILED_OWNER_BLOCKED", "device_policy_blocked"),
        ("Failure [DELETE_FAILED_USER_RESTRICTED]", "DELETE_FAILED_USER_RESTRICTED", "oem_restricted"),
        ("Failure [DELETE_FAILED_ABORTED]", "DELETE_FAILED_ABORTED", "oem_restricted"),
        ("Failure [DELETE_FAILED_INTERNAL_ERROR]", "DELETE_FAILED_INTERNAL_ERROR", "other"),
    ],
)
async def test_uninstall_ks_classifies_pm_uninstall_failure(
    tmp_path, output, expected_code, expected_category
):
    """KSM-BEHAVE-042: an OEM/user restriction is distinguished from a
    device-policy block by pm uninstall's own Failure code, same as
    KSM-BEHAVE-035 already does for pm install."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device,
        "shell",
        new=AsyncMock(
            side_effect=[_NO_ADMINS_BLOCK, "No device owner", "", "", output]
        ),
    ):
        with pytest.raises(PmUninstallFailed) as excinfo:
            await client.uninstall_ks()
    assert excinfo.value.code == expected_code
    assert excinfo.value.category == expected_category


async def test_is_active_admin_true_when_component_listed(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "shell", new=AsyncMock(return_value=_LIVE_DEVICE_POLICY_ADMIN_BLOCK)
    ):
        assert await client.is_active_admin() is True


async def test_is_active_admin_false_when_absent(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value=_NO_ADMINS_BLOCK)):
        assert await client.is_active_admin() is False


async def test_device_owner_component_none_when_dpm_build_unsupported(tmp_path):
    """KSM-BEHAVE-042: live-confirmed against the Test Portal -- `dpm
    get-device-owner` is rejected as an unknown command on this hardware's
    dpm build, and that must read as "can't tell", not as a component."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device,
        "shell",
        new=AsyncMock(return_value=_LIVE_DPM_GET_DEVICE_OWNER_UNSUPPORTED),
    ):
        assert await client.device_owner_component() is None


async def test_device_owner_component_none_when_no_owner_set(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value="No device owner")):
        assert await client.device_owner_component() is None


async def test_device_owner_component_returns_raw_value_when_reported(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    owner_line = "Device owner: me.jxl.kiosk_satellite/.KioskAdminReceiver"
    with patch.object(client._device, "shell", new=AsyncMock(return_value=owner_line)):
        assert await client.device_owner_component() == owner_line


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


async def test_installed_version_reads_versionname_from_dumpsys(tmp_path):
    """KSM-BEHAVE-040 (Phase 2, "install and update"): the authoritative
    installed version, same versionName regex capability_report.py already
    uses, so install_and_launch can decide preserve/repair/verify from the
    device's own package-manager state."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    raw = "Packages:\n  Package [me.jxl.kiosk_satellite] (abc123):\n    versionName=2026.9.64\n"
    with patch.object(client._device, "shell", new=AsyncMock(return_value=raw)) as mock_shell:
        assert await client.installed_version() == "2026.9.64"
    mock_shell.assert_awaited_once_with("dumpsys package me.jxl.kiosk_satellite")


async def test_installed_version_none_when_not_installed(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value="")):
        assert await client.installed_version() is None


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


# KSM-BEHAVE-041 (Phase 3, "permission convergence"): dumpsys/appops/settings
# shapes below are copied verbatim from a live `dumpsys package
# me.jxl.kiosk_satellite` / `cmd appops get` / `dumpsys deviceidle whitelist`
# capture against the Test Portal (2026-09-20), not guessed.
_LIVE_RUNTIME_PERMISSIONS_BLOCK = """\
      runtime permissions:
        android.permission.ACCESS_FINE_LOCATION: granted=true, flags=[ USER_SENSITIVE_WHEN_GRANTED|USER_SENSITIVE_WHEN_DENIED]
        android.permission.READ_EXTERNAL_STORAGE: granted=false, flags=[ USER_SENSITIVE_WHEN_GRANTED|USER_SENSITIVE_WHEN_DENIED]
        android.permission.CAMERA: granted=true, flags=[ USER_SENSITIVE_WHEN_GRANTED|USER_SENSITIVE_WHEN_DENIED]
"""

_LIVE_SERVICE_RESOLVER_BLOCK = """\
Service Resolver Table:
  Non-Data Actions:
      android.accessibilityservice.AccessibilityService:
        e7bceb2 me.jxl.kiosk_satellite/.KioskAccessibilityService filter f02565f permission android.permission.BIND_ACCESSIBILITY_SERVICE
          Action: "android.accessibilityservice.AccessibilityService"
      com.google.android.gms.metadata.MODULE_DEPENDENCIES:
        35d6e03 me.jxl.kiosk_satellite/com.google.android.gms.metadata.ModuleDependencies filter 6c9e8ac
          Action: "com.google.android.gms.metadata.MODULE_DEPENDENCIES"
"""


async def test_granted_permissions_returns_only_granted_true(tmp_path):
    """KSM-TEST-043: `pm grant`'s own exit code is never evidence -- the
    `granted=true`/`granted=false` readback in `dumpsys package` is."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "shell", new=AsyncMock(return_value=_LIVE_RUNTIME_PERMISSIONS_BLOCK)
    ):
        granted = await client.granted_permissions()
    assert granted == {
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.CAMERA",
    }
    assert "android.permission.READ_EXTERNAL_STORAGE" not in granted


@pytest.mark.parametrize(
    "output,expected",
    [
        ("SYSTEM_ALERT_WINDOW: allow", "allow"),
        ("WRITE_SETTINGS: ignore", "ignore"),
        ("GET_USAGE_STATS: deny", "deny"),
        ("", "unknown"),
    ],
)
async def test_appop_mode_parses_cmd_appops_get_output(tmp_path, output, expected):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value=output)) as mock_shell:
        assert await client.appop_mode("SYSTEM_ALERT_WINDOW") == expected
    mock_shell.assert_awaited_once_with("cmd appops get me.jxl.kiosk_satellite SYSTEM_ALERT_WINDOW")


async def test_is_battery_exempt_true_when_package_in_whitelist(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "shell", new=AsyncMock(return_value="user,me.jxl.kiosk_satellite,10133\n")
    ) as mock_shell:
        assert await client.is_battery_exempt() is True
    mock_shell.assert_awaited_once_with("dumpsys deviceidle whitelist")


async def test_is_battery_exempt_false_when_package_absent(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "shell", new=AsyncMock(return_value="user,com.other.app,10001\n")
    ):
        assert await client.is_battery_exempt() is False


async def test_declared_bound_services_extracts_accessibility_component(tmp_path):
    """KSM-TEST-043: the accessibility-service component name is read from
    the device's own declared intent filters, never hardcoded/guessed --
    `docs/developer/android-support/app-lifecycle.md` explicitly warns
    against guessing this string."""
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "shell", new=AsyncMock(return_value=_LIVE_SERVICE_RESOLVER_BLOCK)
    ):
        declared = await client.declared_bound_services()
    assert declared == {
        "android.permission.BIND_ACCESSIBILITY_SERVICE": "me.jxl.kiosk_satellite/.KioskAccessibilityService"
    }


async def test_declared_bound_services_empty_when_none_declared(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value="nothing here\n")):
        assert await client.declared_bound_services() == {}


async def test_get_secure_setting_strips_null_sentinel(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value="null\n")):
        assert await client.get_secure_setting("enabled_notification_listeners") == ""


async def test_get_secure_setting_returns_raw_value(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(
        client._device, "shell", new=AsyncMock(return_value="com.a/.Svc:com.b/.Svc\n")
    ) as mock_shell:
        assert await client.get_secure_setting("enabled_accessibility_services") == "com.a/.Svc:com.b/.Svc"
    mock_shell.assert_awaited_once_with("settings get secure enabled_accessibility_services")


async def test_put_secure_setting_sends_value(tmp_path):
    key_path = ensure_adb_key(str(tmp_path / "keys"))
    client = AdbClient("1.2.3.4", 5555, key_path)
    with patch.object(client._device, "shell", new=AsyncMock(return_value="")) as mock_shell:
        await client.put_secure_setting("enabled_accessibility_services", "com.a/.Svc:com.b/.Svc")
    mock_shell.assert_awaited_once_with(
        "settings put secure enabled_accessibility_services com.a/.Svc:com.b/.Svc"
    )
