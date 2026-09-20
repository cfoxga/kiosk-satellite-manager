"""Unit tests for the shared install/launch/permission-grant/onboarding
sequence (KSM-BEHAVE-007/008/010/011).

Exact install/launch/grant command order and content were confirmed live:
`am start` is required for /api/health to ever come up after `pm install`
(the Test Portal stayed unreachable on :2324 with the app installed but not
running until the activity was launched), and the two ADB fallback grant
commands were pulled verbatim from Kiosk Satellite's own web wizard source
(wizard.js, fetched live off the Test Portal's :2324 web UI), not guessed.

The device/HA sync step (KSM-BEHAVE-010/011) is tested here against the
`ks_api_client` module boundary and HA's auth manager -- the live device-side
web UI contract itself (api/setup/password, PATCH /api/settings,
haCheckConnection) is exercised in test_ks_api_client.py against the same
live-extracted call shapes.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.kiosk_satellite_manager.adb_client import PmInstallFailed
from custom_components.kiosk_satellite_manager.install import (
    KsInstallVerificationFailed,
    install_and_launch,
)
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

_TARGET_VERSION = "2026.9.99"


class _FakeHass:
    """Just enough of HomeAssistant for install_and_launch's executor-job
    and (when a password is set) auth-manager calls -- a unit test has no
    real hass fixture (see tests/conftest.py)."""

    def __init__(self) -> None:
        self.auth = MagicMock()
        self.auth.async_get_owner = AsyncMock(return_value="the-owner")
        self.auth.async_create_refresh_token = AsyncMock(return_value="the-refresh-token")
        self.auth.async_create_access_token = MagicMock(return_value="minted-ha-token")

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _fake_apk_response():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.read = AsyncMock(return_value=b"fake-apk-bytes")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_health_response(app_version=_TARGET_VERSION):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value={"appVersion": app_version})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_client():
    client = MagicMock()
    client.getprop = AsyncMock(return_value="arm64-v8a")
    client.push = AsyncMock()
    client.install_apk = AsyncMock()
    # KSM-BEHAVE-039: pre-install check (not installed/unknown), then
    # post-install verify matching the release fetched by _fake_session's
    # default latest_release patch below -- the common "fresh install
    # succeeds" shape every pre-existing test in this file exercises.
    client.installed_version = AsyncMock(side_effect=[None, _TARGET_VERSION])
    client.shell = AsyncMock(return_value="")
    return client


def _fake_session(app_version=_TARGET_VERSION):
    """session.get is URL-aware: the releases-asset download and
    /api/health (KSM-BEHAVE-039's postcondition readback) share one mock
    session but must return different response shapes."""
    session = MagicMock()

    def _get(url, **kwargs):
        if "/api/health" in url:
            return _fake_health_response(app_version)
        return _fake_apk_response()

    session.get = MagicMock(side_effect=_get)
    return session


async def test_install_and_launch_runs_expected_shell_sequence():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        await install_and_launch(hass, client, session)

    assert client.push.await_count == 1
    client.install_apk.assert_awaited_once()
    assert client.install_apk.await_args.args[0].startswith("/data/local/tmp/")
    shell_calls = [c.args[0] for c in client.shell.await_args_list]
    assert shell_calls[0].startswith("rm -f ")
    assert shell_calls[1] == "am start -n me.jxl.kiosk_satellite/.MainActivity"
    assert "pm grant me.jxl.kiosk_satellite android.permission.RECORD_AUDIO" in shell_calls
    assert "pm grant me.jxl.kiosk_satellite android.permission.READ_LOGS" in shell_calls
    assert "appops set me.jxl.kiosk_satellite SYSTEM_ALERT_WINDOW allow" in shell_calls
    assert "dumpsys deviceidle whitelist +me.jxl.kiosk_satellite" in shell_calls
    assert "dpm set-active-admin me.jxl.kiosk_satellite/.KioskAdminReceiver" in shell_calls
    assert "settings put global package_verifier_enable 0" in shell_calls


async def test_install_and_launch_aborts_before_launch_on_rejected_artifact():
    """KSM-BEHAVE-035: a device that rejects the artifact (old SDK,
    incompatible ABI, insufficient storage, or a signing-cert mismatch) must
    not get am start/permission-grant commands run against a package that
    was never actually installed -- and the pushed APK on-device must still
    be cleaned up."""
    hass = _FakeHass()
    client = _fake_client()
    client.install_apk = AsyncMock(side_effect=PmInstallFailed("INSTALL_FAILED_OLDER_SDK_VERSION", "unsupported_sdk"))
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        with pytest.raises(PmInstallFailed) as exc_info:
            await install_and_launch(hass, client, session)

    assert exc_info.value.category == "unsupported_sdk"
    shell_calls = [c.args[0] for c in client.shell.await_args_list]
    assert len(shell_calls) == 1
    assert shell_calls[0].startswith("rm -f ")


async def test_install_and_launch_uses_device_profile_for_start_url_and_permissions():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api, patch(
        "custom_components.kiosk_satellite_manager.install.get_url",
        return_value="http://192.168.1.2:8123",
    ):
        mock_api.get_setup_status = AsyncMock(
            return_value={"passwordNeeded": True, "deviceName": "Test"}
        )
        mock_api.setup_password = AsyncMock(return_value="token123")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=True)

        await install_and_launch(
            hass,
            client,
            session,
            host="192.168.1.50",
            device_name="TV Stick",
            password="1newpass",
            device_profile="gtv_stick",
        )

        patch_args = mock_api.patch_settings.await_args[0]
        settings_payload = patch_args[3]
        assert settings_payload["browser.start_url"] == "http://192.168.1.2:8123"

        shell_calls = [c.args[0] for c in client.shell.await_args_list]
        assert "dpm set-active-admin me.jxl.kiosk_satellite/.KioskAdminReceiver" not in shell_calls



async def test_install_and_launch_skips_sync_when_no_password_configured():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api:
        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password=None
        )

    mock_api.get_setup_status.assert_not_called()
    hass.auth.async_get_owner.assert_not_called()


async def test_install_and_launch_syncs_password_and_name_on_first_run():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api, patch(
        "custom_components.kiosk_satellite_manager.install.get_url",
        return_value="http://192.168.1.2:8123",
    ):
        mock_api.get_setup_status = AsyncMock(
            return_value={"passwordNeeded": True, "deviceName": "unconfigured"}
        )
        mock_api.setup_password = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=True)

        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22"
        )

    mock_api.setup_password.assert_awaited_once_with(session, "192.168.1.50", "hunter22", "Kitchen")
    mock_api.login.assert_not_called()
    hass.auth.async_get_owner.assert_awaited_once()
    mock_api.patch_settings.assert_awaited_once_with(
        session,
        "192.168.1.50",
        "ks-token",
        {
            "ha.url": "http://192.168.1.2:8123",
            "ha.token": "minted-ha-token",
            "browser.start_url": "http://192.168.1.2:8123/portal",
            "browser.ignore_ssl_errors": True,
            "home.enabled": True,
        },
    )
    mock_api.check_ha_connection.assert_awaited_once_with(session, "192.168.1.50", "ks-token")


async def test_install_and_launch_mints_a_unique_managed_token_name():
    """[KSM-TEST-017] Retrying a reset device must not collide with old tokens."""
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api, patch(
        "custom_components.kiosk_satellite_manager.install.get_url",
        return_value="http://192.168.1.2:8123",
    ):
        mock_api.get_setup_status = AsyncMock(return_value={"passwordNeeded": True})
        mock_api.setup_password = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=True)

        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22"
        )

    client_name = hass.auth.async_create_refresh_token.await_args.kwargs["client_name"]
    assert client_name.startswith("Kiosk Satellite Manager - Kitchen [")
    assert client_name.endswith("]")


async def test_install_and_launch_logs_in_and_patches_name_when_password_already_set():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api, patch(
        "custom_components.kiosk_satellite_manager.install.get_url",
        return_value="http://192.168.1.2:8123",
    ):
        mock_api.get_setup_status = AsyncMock(
            return_value={"passwordNeeded": False, "deviceName": "old-name"}
        )
        mock_api.login = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=True)

        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22"
        )

    mock_api.setup_password.assert_not_called()
    mock_api.login.assert_awaited_once_with(session, "192.168.1.50", "hunter22")
    name_patch_call = mock_api.patch_settings.await_args_list[0]
    assert name_patch_call.args[3] == {"device.name": "Kitchen"}


async def test_install_and_launch_does_not_repatch_name_when_already_correct():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api, patch(
        "custom_components.kiosk_satellite_manager.install.get_url",
        return_value="http://192.168.1.2:8123",
    ):
        mock_api.get_setup_status = AsyncMock(
            return_value={"passwordNeeded": False, "deviceName": "Kitchen"}
        )
        mock_api.login = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=True)

        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22"
        )

    # only the settings patch, not a redundant device.name patch
    assert mock_api.patch_settings.await_count == 1
    assert mock_api.patch_settings.await_args_list[0].args[3] == {
        "ha.url": "http://192.168.1.2:8123",
        "ha.token": "minted-ha-token",
        "browser.start_url": "http://192.168.1.2:8123/portal",
        "browser.ignore_ssl_errors": True,
        "home.enabled": True,
    }


async def test_install_and_launch_sync_failure_is_logged_not_raised():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api:
        mock_api.get_setup_status = AsyncMock(side_effect=KsApiError("web UI not up yet"))

        # must not raise -- install already succeeded by this point
        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22"
        )

    assert client.shell.await_count == 15


async def test_install_and_launch_reuses_provided_token_and_respects_launcher_flag():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api, patch(
        "custom_components.kiosk_satellite_manager.install.get_url",
        return_value="http://192.168.1.2:8123",
    ):
        mock_api.get_setup_status = AsyncMock(
            return_value={"passwordNeeded": False, "deviceName": "Kitchen"}
        )
        mock_api.login = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=True)

        res = await install_and_launch(
            hass,
            client,
            session,
            host="192.168.1.50",
            device_name="Kitchen",
            password="hunter22",
            ha_token="pre-existing-token",
            home_launcher=False,
        )

    assert res == "pre-existing-token"
    hass.auth.async_get_owner.assert_not_called()
    assert mock_api.patch_settings.await_args_list[0].args[3] == {
        "ha.url": "http://192.168.1.2:8123",
        "ha.token": "pre-existing-token",
        "browser.start_url": "http://192.168.1.2:8123/portal",
        "browser.ignore_ssl_errors": True,
    }


async def test_install_and_launch_skips_install_when_already_at_target_version():
    """KSM-BEHAVE-039 (Phase 2, "preserve compatible installations where
    possible"): a device already running the release we'd fetch must not be
    reinstalled -- but am start/permission grants still run every press."""
    hass = _FakeHass()
    client = _fake_client()
    client.installed_version = AsyncMock(return_value=_TARGET_VERSION)
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        await install_and_launch(hass, client, session)

    client.push.assert_not_called()
    client.install_apk.assert_not_called()
    shell_calls = [c.args[0] for c in client.shell.await_args_list]
    assert not any(call.startswith("rm -f ") for call in shell_calls)
    assert shell_calls[0] == "am start -n me.jxl.kiosk_satellite/.MainActivity"


async def test_install_and_launch_repairs_via_uninstall_on_signature_mismatch():
    """KSM-BEHAVE-039 (Phase 2, "choose... repair... from observed state"):
    a signing-cert/update mismatch on a device with an existing install is
    recovered by uninstalling and retrying once, not a hard failure."""
    hass = _FakeHass()
    client = _fake_client()
    client.installed_version = AsyncMock(side_effect=["9.0.0", _TARGET_VERSION])
    client.install_apk = AsyncMock(
        side_effect=[
            PmInstallFailed("INSTALL_FAILED_UPDATE_INCOMPATIBLE", "incompatible_signature"),
            None,
        ]
    )
    client.uninstall_ks = AsyncMock()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        await install_and_launch(hass, client, session)

    client.uninstall_ks.assert_awaited_once()
    assert client.install_apk.await_count == 2


async def test_install_and_launch_does_not_repair_signature_mismatch_on_fresh_device():
    """A device that never had Kiosk Satellite installed has nothing to
    repair by uninstalling -- still a hard failure, matching Phase 1."""
    hass = _FakeHass()
    client = _fake_client()
    client.installed_version = AsyncMock(return_value=None)
    client.install_apk = AsyncMock(
        side_effect=PmInstallFailed("INSTALL_FAILED_UPDATE_INCOMPATIBLE", "incompatible_signature")
    )
    client.uninstall_ks = AsyncMock()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        with pytest.raises(PmInstallFailed):
            await install_and_launch(hass, client, session)

    client.uninstall_ks.assert_not_called()
    assert client.install_apk.await_count == 1


async def test_install_and_launch_raises_when_installed_version_mismatches_after_install():
    hass = _FakeHass()
    client = _fake_client()
    client.installed_version = AsyncMock(side_effect=[None, "stale-version"])
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        with pytest.raises(KsInstallVerificationFailed):
            await install_and_launch(hass, client, session)


async def test_install_and_launch_raises_when_am_start_reports_error():
    hass = _FakeHass()
    client = _fake_client()
    client.shell = AsyncMock(
        side_effect=[
            "",  # rm -f
            "Error: Activity class does not exist",  # am start
        ]
    )
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        with pytest.raises(KsInstallVerificationFailed):
            await install_and_launch(hass, client, session)


async def test_install_and_launch_raises_when_health_never_confirms_after_install():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session(app_version="wrong-version")

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.asyncio.sleep",
        new=AsyncMock(),
    ):
        with pytest.raises(KsInstallVerificationFailed):
            await install_and_launch(hass, client, session, host="192.168.1.50")
