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

from custom_components.kiosk_satellite_manager.install import install_and_launch
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError


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


def _fake_client():
    client = MagicMock()
    client.getprop = AsyncMock(return_value="arm64-v8a")
    client.push = AsyncMock()
    client.shell = AsyncMock(return_value="")
    return client


def _fake_session():
    session = MagicMock()
    session.get = MagicMock(return_value=_fake_apk_response())
    return session


async def test_install_and_launch_runs_expected_shell_sequence():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_apk_url",
        new=AsyncMock(return_value="https://example.invalid/ks.apk"),
    ):
        await install_and_launch(hass, client, session)

    assert client.push.await_count == 1
    shell_calls = [c.args[0] for c in client.shell.await_args_list]
    assert shell_calls[0].startswith("pm install -r -g ")
    assert shell_calls[1].startswith("rm -f ")
    assert shell_calls[2] == "am start -n me.jxl.kiosk_satellite/.MainActivity"
    assert "pm grant me.jxl.kiosk_satellite android.permission.RECORD_AUDIO" in shell_calls
    assert "pm grant me.jxl.kiosk_satellite android.permission.READ_LOGS" in shell_calls
    assert "appops set me.jxl.kiosk_satellite SYSTEM_ALERT_WINDOW allow" in shell_calls
    assert "dumpsys deviceidle whitelist +me.jxl.kiosk_satellite" in shell_calls
    assert "dpm set-active-admin me.jxl.kiosk_satellite/.KioskAdminReceiver" in shell_calls
    assert "settings put global package_verifier_enable 0" in shell_calls


async def test_install_and_launch_uses_device_profile_for_start_url_and_permissions():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_apk_url",
        new=AsyncMock(return_value="https://example.invalid/ks.apk"),
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
        "custom_components.kiosk_satellite_manager.install.latest_apk_url",
        new=AsyncMock(return_value="https://example.invalid/ks.apk"),
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
        "custom_components.kiosk_satellite_manager.install.latest_apk_url",
        new=AsyncMock(return_value="https://example.invalid/ks.apk"),
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
        "custom_components.kiosk_satellite_manager.install.latest_apk_url",
        new=AsyncMock(return_value="https://example.invalid/ks.apk"),
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
        "custom_components.kiosk_satellite_manager.install.latest_apk_url",
        new=AsyncMock(return_value="https://example.invalid/ks.apk"),
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
        "custom_components.kiosk_satellite_manager.install.latest_apk_url",
        new=AsyncMock(return_value="https://example.invalid/ks.apk"),
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
        "custom_components.kiosk_satellite_manager.install.latest_apk_url",
        new=AsyncMock(return_value="https://example.invalid/ks.apk"),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api:
        mock_api.get_setup_status = AsyncMock(side_effect=KsApiError("web UI not up yet"))

        # must not raise -- install already succeeded by this point
        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22"
        )

    assert client.shell.await_count == 16


async def test_install_and_launch_reuses_provided_token_and_respects_launcher_flag():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_apk_url",
        new=AsyncMock(return_value="https://example.invalid/ks.apk"),
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
