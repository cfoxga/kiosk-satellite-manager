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

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.kiosk_satellite_manager.adb_client import PmInstallFailed
from custom_components.kiosk_satellite_manager.apk_signing import ApkSignerVerificationFailed
from custom_components.kiosk_satellite_manager.device_catalog import NoApprovedRecipe
from custom_components.kiosk_satellite_manager.install_recipes import get_recipe
from custom_components.kiosk_satellite_manager.install import (
    KsInstallVerificationFailed,
    PermissionConvergenceResult,
    _verify_health,
    _wait_for_setup_status,
    converge_permissions,
    install_and_launch,
    verify_functional_capabilities,
)
from custom_components.kiosk_satellite_manager.credentials import TokenCredential
from custom_components.kiosk_satellite_manager.const import SYNC_STATUS_POLL_ATTEMPTS
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError
from homeassistant.auth.const import GROUP_ID_READ_ONLY

_TARGET_VERSION = "2026.9.99"

# KSM-BEHAVE-048 (issue #20): these lists used to be module constants in
# install.py applied to *any* device, matched or not. They are now the
# SDK-29 output of one named recipe version, and a device only gets them by
# resolving to a model with an approved assignment to that recipe. The
# values below are unchanged -- that is the point of KSM-TEST-063.
PORTAL_RECIPE = get_recipe("meta_portal_standard", "v3")
LAUNCHER_RECIPE = get_recipe("meta_portal_standard", "v2")
PORTAL_PERMISSIONS = PORTAL_RECIPE.permissions_for_sdk(29)
PORTAL_APPOPS = PORTAL_RECIPE.appops_for_sdk(29)


class _FakeHass:
    """Just enough of HomeAssistant for install_and_launch's executor-job
    and (when a password is set) auth-manager calls -- a unit test has no
    real hass fixture (see tests/conftest.py)."""

    def __init__(self) -> None:
        self.auth = MagicMock()
        self.auth.async_get_owner = AsyncMock(return_value="the-owner")
        self.auth.async_create_user = AsyncMock(return_value="the-kiosk-user")
        self.auth.async_create_refresh_token = AsyncMock(
            return_value=SimpleNamespace(id="the-refresh-token")
        )
        self.auth.async_create_access_token = MagicMock(return_value="minted-ha-token")
        self.auth.async_get_refresh_token = MagicMock(return_value=SimpleNamespace(id="the-refresh-token"))
        self.auth.async_remove_refresh_token = MagicMock()

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
    # KSM-BEHAVE-040: pre-install check (not installed/unknown), then
    # post-install verify matching the release fetched by _fake_session's
    # default latest_release patch below -- the common "fresh install
    # succeeds" shape every pre-existing test in this file exercises.
    client.installed_version = AsyncMock(side_effect=[None, _TARGET_VERSION])
    client.shell = AsyncMock(return_value="")
    # KSM-BEHAVE-041: default "everything converges cleanly" readback for
    # the meta_portal_standard:v2 permission/appops set -- individual
    # convergence tests override these to exercise the
    # partial/needs-user-interaction paths.
    client.granted_permissions = AsyncMock(return_value=set(PORTAL_PERMISSIONS))
    client.appop_mode = AsyncMock(return_value="allow")
    client.is_battery_exempt = AsyncMock(return_value=True)
    client.declared_bound_services = AsyncMock(return_value={})
    client.get_secure_setting = AsyncMock(return_value="")
    client.put_secure_setting = AsyncMock()
    # KSM-BEHAVE-046 (Phase 5): device-level Bluetooth radio state, distinct
    # from any per-app permission -- defaults "on" since PORTAL_PERMISSIONS
    # requests no BLUETOOTH_SCAN/CONNECT at all (sdk 29 in these tests).
    client.bluetooth_enabled = AsyncMock(return_value=True)
    client.select_ks_home = AsyncMock()
    client.resolved_home_activity = AsyncMock(
        return_value="me.jxl.kiosk_satellite/.HomeAlias"
    )
    return client


def _fake_session(app_version=_TARGET_VERSION):
    """session.get is URL-aware: the releases-asset download and
    /api/health (KSM-BEHAVE-040's postcondition readback) share one mock
    session but must return different response shapes."""
    session = MagicMock()

    def _get(url, **kwargs):
        if "/api/health" in url:
            return _fake_health_response(app_version)
        return _fake_apk_response()

    session.get = MagicMock(side_effect=_get)
    return session


@pytest.fixture(autouse=True)
def _ks_without_tls(monkeypatch):
    """These tests predate #57: model a KS release without HTTPS support so
    onboarding keeps its HTTP path. TLS onboarding is test_ks_tls.py."""
    monkeypatch.setattr(
        "custom_components.kiosk_satellite_manager.install.ks_tls.async_establish_tls",
        AsyncMock(return_value=None),
    )

    async def _http_health(session, host):
        # The HTTP-device leg of _read_health_any, served by _fake_session.
        async with session.get(f"http://{host}:2324/api/health") as resp:
            resp.raise_for_status()
            return await resp.json()

    monkeypatch.setattr(
        "custom_components.kiosk_satellite_manager.install._read_health_any", _http_health
    )


@pytest.fixture(autouse=True)
def _accept_fake_apk_artifacts(monkeypatch):
    """Existing install-flow tests use arbitrary bytes, not signed APK files."""
    monkeypatch.setattr(
        "custom_components.kiosk_satellite_manager.install.verify_ks_apk_signer", MagicMock()
    )


async def test_install_and_launch_runs_expected_shell_sequence():
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        await install_and_launch(hass, client, session, device_model="portal_go")

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
    # [KSM-TEST-097] Package verification is a device-wide security setting,
    # never an installation convenience.  This covers both the Portal recipe
    # and the installer executor: no install press may disable it.
    assert "settings put global package_verifier_enable 0" not in shell_calls


async def test_launcher_selection_uses_adb_only_and_verifies_home_resolver():
    """[KSM-TEST-125] Launcher selection is independent of HTTP credentials."""
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        await install_and_launch(
            hass,
            client,
            session,
            host="192.168.1.50",
            password=None,
            home_launcher=True,
            device_model="portal_mini",
        )

    provision_calls = [
        call.args[0]
        for call in client.shell.await_args_list
        if "ks.provision" in call.args[0]
    ]
    assert len(provision_calls) == 1
    assert '"home.enabled": true' in provision_calls[0]
    assert "password" not in provision_calls[0]
    assert "ha.token" not in provision_calls[0]
    client.select_ks_home.assert_awaited_once_with()
    client.resolved_home_activity.assert_awaited_once_with()


async def test_launcher_selection_rejects_rival_home_resolver():
    """[KSM-TEST-125] A Meta resolver is the independently failing control."""
    hass = _FakeHass()
    client = _fake_client()
    client.resolved_home_activity = AsyncMock(
        return_value="com.facebook.alohaapps.launcher/.HomeActivity"
    )
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.asyncio.sleep",
        new=AsyncMock(),
    ):
        with pytest.raises(KsInstallVerificationFailed, match="HOME resolver"):
            await install_and_launch(
                hass,
                client,
                session,
                host="192.168.1.50",
                password=None,
                home_launcher=True,
                device_model="portal_mini",
            )


async def test_launcher_selection_rejects_a_different_ks_activity():
    """[KSM-TEST-125] Only HomeAlias satisfies the resolver postcondition."""
    hass = _FakeHass()
    client = _fake_client()
    client.resolved_home_activity = AsyncMock(
        return_value="me.jxl.kiosk_satellite/.MainActivity"
    )

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.asyncio.sleep",
        new=AsyncMock(),
    ):
        with pytest.raises(KsInstallVerificationFailed, match="HOME resolver"):
            await install_and_launch(
                hass,
                client,
                _fake_session(),
                host="192.168.1.50",
                password=None,
                home_launcher=True,
                device_model="portal_mini",
            )


async def test_launcher_selection_skips_disabled_and_incapable_recipes():
    """[KSM-TEST-125] Neither caller opt-out nor Portal TV may mutate HOME."""
    for model, enabled in (
        ("portal_mini", False),
        ("portal_go", True),
        ("portal_tv", True),
    ):
        client = _fake_client()
        with patch(
            "custom_components.kiosk_satellite_manager.install.latest_release",
            new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
        ):
            await install_and_launch(
                _FakeHass(),
                client,
                _fake_session(),
                host="192.168.1.50",
                password=None,
                home_launcher=enabled,
                device_model=model,
            )

        assert all("ks.provision" not in call.args[0] for call in client.shell.await_args_list)
        client.select_ks_home.assert_not_awaited()
        client.resolved_home_activity.assert_not_awaited()


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
            await install_and_launch(hass, client, session, device_model="portal_go")

    assert exc_info.value.category == "unsupported_sdk"
    shell_calls = [c.args[0] for c in client.shell.await_args_list]
    assert len(shell_calls) == 1
    assert shell_calls[0].startswith("rm -f ")


async def test_install_and_launch_uses_the_assigned_recipe_for_start_url_and_launcher():
    """KSM-TEST-058: Portal TV resolves meta_portal_tv:v2, which is the same
    Portal install lifecycle minus the home-launcher takeover. The launcher
    capability must come from the recipe assigned to *this* model, never from
    the caller's home_launcher preference alone -- home_launcher=True below is
    deliberately the permissive input, and the payload must still omit
    home.enabled."""
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
        mock_api.probe_https = AsyncMock(return_value=None)  # device still on HTTP
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
            device_name="Portal TV",
            password="1newpass",
            device_model="portal_tv",
            home_launcher=True,
        )

        settings_payload = mock_api.patch_settings.await_args[0][3]
        assert settings_payload["browser.start_url"] == "http://192.168.1.2:8123/portal"
        assert "home.enabled" not in settings_payload

    # Positive control: Portal Mini retains the launcher-capable v2 recipe;
    # Portal Go's observed OEM resolver is now assigned v3 without takeover.
    assert LAUNCHER_RECIPE.home_launcher_supported is True
    assert PORTAL_RECIPE.home_launcher_supported is False


async def test_install_and_launch_fails_closed_for_a_model_with_no_approved_recipe():
    """KSM-TEST-060: "gtv_stick" is a fallback classification, not an exact
    model, so it has no approved recipe assignment. Before the catalog it
    silently received the Portal permission/device-admin set; now the install
    refuses, and -- critically -- refuses *before* touching the device, so a
    device that cannot be provisioned is also never half-provisioned."""
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        with pytest.raises(NoApprovedRecipe):
            await install_and_launch(
                hass, client, session, host="192.168.1.50", device_model="gtv_stick"
            )

    client.push.assert_not_awaited()
    client.install_apk.assert_not_awaited()
    client.shell.assert_not_awaited()


async def test_install_and_launch_fails_closed_when_no_model_matched():
    """KSM-TEST-060, the unmatched-device case: device_model=None is what a
    config entry created from unreadable identity facts carries. There is no
    default recipe to fall back to."""
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with pytest.raises(NoApprovedRecipe):
        await install_and_launch(hass, client, session, host="192.168.1.50")

    client.install_apk.assert_not_awaited()
    client.shell.assert_not_awaited()


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
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password=None,
            device_model="portal_go",
        )

    mock_api.get_setup_status.assert_not_called()
    hass.auth.async_get_owner.assert_not_called()


async def test_install_and_launch_syncs_password_and_name_on_first_run():
    """[KSM-TEST-109] Automatic minting creates a dedicated kiosk identity."""
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
        mock_api.probe_https = AsyncMock(return_value=None)  # device still on HTTP
        mock_api.get_setup_status = AsyncMock(
            return_value={"passwordNeeded": True, "deviceName": "unconfigured"}
        )
        mock_api.setup_password = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=True)

        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22",
            device_model="portal_go",
        )

    mock_api.setup_password.assert_awaited_once_with(session, "192.168.1.50", "hunter22", "Kitchen", pin=None)
    mock_api.login.assert_not_called()
    hass.auth.async_get_owner.assert_not_called()
    hass.auth.async_create_user.assert_awaited_once_with(
        "Kiosk Satellite - Kitchen", group_ids=[GROUP_ID_READ_ONLY], local_only=True
    )
    mock_api.patch_settings.assert_awaited_once_with(
        session,
        "192.168.1.50",
        "ks-token",
        {
            "ha.url": "http://192.168.1.2:8123",
            "ha.token": "minted-ha-token",
            "browser.start_url": "http://192.168.1.2:8123/portal",
            "browser.ignore_ssl_errors": False,
        },
        pin=None,
    )
    mock_api.check_ha_connection.assert_awaited_once_with(session, "192.168.1.50", "ks-token", pin=None)


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
        mock_api.probe_https = AsyncMock(return_value=None)  # device still on HTTP
        mock_api.get_setup_status = AsyncMock(return_value={"passwordNeeded": True})
        mock_api.setup_password = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=True)

        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22",
            device_model="portal_go",
        )

    client_name = hass.auth.async_create_refresh_token.await_args.kwargs["client_name"]
    assert client_name.startswith("Kiosk Satellite Manager - Kitchen [")
    assert client_name.endswith("]")


async def test_install_failure_after_mint_revokes_the_owned_refresh_token():
    """[KSM-TEST-099] A sync failure cannot orphan KSM's newly minted token."""
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
        mock_api.probe_https = AsyncMock(return_value=None)  # device still on HTTP
        mock_api.get_setup_status = AsyncMock(return_value={"passwordNeeded": True})
        mock_api.setup_password = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(side_effect=KsApiError("offline"))

        result = await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22",
            device_model="portal_go",
        )

    assert result is None
    hass.auth.async_remove_refresh_token.assert_called_once_with(
        hass.auth.async_get_refresh_token.return_value
    )


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
        mock_api.probe_https = AsyncMock(return_value=None)  # device still on HTTP
        mock_api.get_setup_status = AsyncMock(
            return_value={"passwordNeeded": False, "deviceName": "old-name"}
        )
        mock_api.login = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=True)

        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22",
            device_model="portal_go",
        )

    mock_api.setup_password.assert_not_called()
    mock_api.login.assert_awaited_once_with(session, "192.168.1.50", "hunter22", pin=None)
    name_patch_call = mock_api.patch_settings.await_args_list[0]
    assert name_patch_call.args[3] == {"device.name": "Kitchen"}


async def test_install_and_launch_does_not_repatch_name_when_already_correct():
    """[KSM-TEST-096] Existing-device sync cannot retain the legacy bypass."""
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
        mock_api.probe_https = AsyncMock(return_value=None)  # device still on HTTP
        mock_api.get_setup_status = AsyncMock(
            return_value={"passwordNeeded": False, "deviceName": "Kitchen"}
        )
        mock_api.login = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=True)

        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22",
            device_model="portal_go",
        )

    # only the settings patch, not a redundant device.name patch
    assert mock_api.patch_settings.await_count == 1
    assert mock_api.patch_settings.await_args_list[0].args[3] == {
        "ha.url": "http://192.168.1.2:8123",
        "ha.token": "minted-ha-token",
        "browser.start_url": "http://192.168.1.2:8123/portal",
        "browser.ignore_ssl_errors": False,
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
        mock_api.probe_https = AsyncMock(return_value=None)  # device still on HTTP
        mock_api.get_setup_status = AsyncMock(side_effect=KsApiError("web UI not up yet"))

        # must not raise -- install already succeeded by this point
        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen", password="hunter22",
            device_model="portal_go",
        )

    # 15, not the pre-catalog 14: this test used to run with no device_profile
    # at all, which took the unmatched-device fallback and granted the generic
    # 7-permission list. portal_go always granted 8 (the Portal-only
    # WRITE_SECURE_SETTINGS) -- the count moved because the fallback path is
    # gone. Package verification remains enabled (KSM-BEHAVE-062), and the
    # Portal Go v3 recipe does not issue a launcher mutation (KSM-BEHAVE-069).
    assert client.shell.await_count == 15
    grants = [c.args[0] for c in client.shell.await_args_list if c.args[0].startswith("pm grant ")]
    assert f"pm grant me.jxl.kiosk_satellite android.permission.WRITE_SECURE_SETTINGS" in grants


async def test_portal_go_shell_sequence_excludes_verifier_disable():
    """[KSM-TEST-097] The Portal sequence retains its explicit grants but
    excludes the device-wide package-verifier mutation."""
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        await install_and_launch(hass, client, session, device_model="portal_go")

    shell_calls = [c.args[0] for c in client.shell.await_args_list]
    assert shell_calls[1:] == [
        "am start -n me.jxl.kiosk_satellite/.MainActivity",
        "pm grant me.jxl.kiosk_satellite android.permission.RECORD_AUDIO",
        "pm grant me.jxl.kiosk_satellite android.permission.CAMERA",
        "pm grant me.jxl.kiosk_satellite android.permission.ACCESS_COARSE_LOCATION",
        "pm grant me.jxl.kiosk_satellite android.permission.ACCESS_FINE_LOCATION",
        "pm grant me.jxl.kiosk_satellite android.permission.READ_LOGS",
        "pm grant me.jxl.kiosk_satellite android.permission.READ_EXTERNAL_STORAGE",
        "pm grant me.jxl.kiosk_satellite android.permission.WRITE_EXTERNAL_STORAGE",
        "pm grant me.jxl.kiosk_satellite android.permission.WRITE_SECURE_SETTINGS",
        "appops set me.jxl.kiosk_satellite SYSTEM_ALERT_WINDOW allow",
        "appops set me.jxl.kiosk_satellite WRITE_SETTINGS allow",
        "appops set me.jxl.kiosk_satellite GET_USAGE_STATS allow",
        "dumpsys deviceidle whitelist +me.jxl.kiosk_satellite",
        "dpm set-active-admin me.jxl.kiosk_satellite/.KioskAdminReceiver",
    ]
    assert shell_calls[0].startswith("rm -f ")


async def test_install_and_launch_reuses_provided_token_and_respects_launcher_flag():
    """[KSM-TEST-096/142] Reused token and selected HA URL reach the device."""
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
        mock_api.probe_https = AsyncMock(return_value=None)  # device still on HTTP
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
            device_model="portal_go",
            ha_token="pre-existing-token",
            ha_url="https://ha.example.test",
            home_launcher=False,
        )

    assert res == TokenCredential("pre-existing-token", None, owned=False)
    hass.auth.async_get_owner.assert_not_called()
    assert mock_api.patch_settings.await_args_list[0].args[3] == {
        "ha.url": "https://ha.example.test",
        "ha.token": "pre-existing-token",
        "browser.start_url": "https://ha.example.test/portal",
        "browser.ignore_ssl_errors": False,
    }


async def test_install_and_launch_skips_install_when_already_at_target_version():
    """KSM-BEHAVE-040 (Phase 2, "preserve compatible installations where
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
        await install_and_launch(hass, client, session, device_model="portal_go")

    client.push.assert_not_called()
    client.install_apk.assert_not_called()
    shell_calls = [c.args[0] for c in client.shell.await_args_list]
    assert not any(call.startswith("rm -f ") for call in shell_calls)
    assert shell_calls[0] == "am start -n me.jxl.kiosk_satellite/.MainActivity"


async def test_install_and_launch_never_uninstalls_on_signature_mismatch():
    """[KSM-TEST-098] Android's incompatible-signature response is not a
    recovery signal: uninstalling would bypass certificate continuity and
    discard the installed app's data."""
    hass = _FakeHass()
    client = _fake_client()
    client.installed_version = AsyncMock(return_value="9.0.0")
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
            await install_and_launch(hass, client, session, device_model="portal_go")

    client.uninstall_ks.assert_not_awaited()
    client.install_apk.assert_awaited_once()


async def test_install_and_launch_rejects_untrusted_apk_before_device_mutation():
    """[KSM-TEST-098] An artifact outside the reviewed signer policy cannot
    be pushed, installed, uninstalled around, launched, or granted access."""
    hass = _FakeHass()
    client = _fake_client()
    client.uninstall_ks = AsyncMock()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.verify_ks_apk_signer",
        side_effect=ApkSignerVerificationFailed("APK signer is not trusted by KSM policy"),
    ) as verify:
        with pytest.raises(ApkSignerVerificationFailed, match="not trusted"):
            await install_and_launch(hass, client, session, device_model="portal_go")

    verify.assert_called_once_with(b"fake-apk-bytes")
    client.push.assert_not_awaited()
    client.install_apk.assert_not_awaited()
    client.uninstall_ks.assert_not_awaited()
    client.shell.assert_not_awaited()


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
            await install_and_launch(hass, client, session, device_model="portal_go")

    client.uninstall_ks.assert_not_called()
    assert client.install_apk.await_count == 1


async def test_install_uses_sdk_29_fallback_when_the_sdk_probe_fails():
    """[KSM-TEST-113] A failed SDK probe takes the documented safe fallback."""
    hass = _FakeHass()
    client = _fake_client()
    client.getprop = AsyncMock(side_effect=["arm64-v8a", OSError("probe failed")])
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        await install_and_launch(hass, client, session, device_model="portal_go")

    shell_calls = [call.args[0] for call in client.shell.await_args_list]
    assert "pm grant me.jxl.kiosk_satellite android.permission.WRITE_EXTERNAL_STORAGE" in shell_calls
    assert "pm grant me.jxl.kiosk_satellite android.permission.BLUETOOTH_SCAN" not in shell_calls


async def test_health_poll_retries_transport_failure_then_returns_on_matching_readback(monkeypatch):
    """[KSM-TEST-114] Health retries only after a transport failure."""
    fetch = AsyncMock(side_effect=[aiohttp.ClientConnectionError("refused"), {"appVersion": _TARGET_VERSION}])
    sleep = AsyncMock()
    monkeypatch.setattr("custom_components.kiosk_satellite_manager.install._read_health_any", fetch)
    monkeypatch.setattr("custom_components.kiosk_satellite_manager.install.asyncio.sleep", sleep)

    await _verify_health(MagicMock(), "192.168.1.50", _TARGET_VERSION)

    assert fetch.await_count == 2
    sleep.assert_awaited_once()


async def test_health_poll_reports_the_last_failed_readback(monkeypatch):
    """[KSM-TEST-114] A reachable but wrong app version cannot pass health verification."""
    fetch = AsyncMock(return_value={"appVersion": "stale"})
    sleep = AsyncMock()
    monkeypatch.setattr("custom_components.kiosk_satellite_manager.install._read_health_any", fetch)
    monkeypatch.setattr("custom_components.kiosk_satellite_manager.install.asyncio.sleep", sleep)

    with pytest.raises(KsInstallVerificationFailed, match="appVersion='stale'"):
        await _verify_health(MagicMock(), "192.168.1.50", _TARGET_VERSION)

    assert fetch.await_count == SYNC_STATUS_POLL_ATTEMPTS
    assert sleep.await_count == SYNC_STATUS_POLL_ATTEMPTS - 1


async def test_setup_status_poll_retries_and_exhaustion_is_actionable(monkeypatch):
    """[KSM-TEST-115] Setup-status sync is bounded for both recovery and failure."""
    sleep = AsyncMock()
    status = AsyncMock(side_effect=[asyncio.TimeoutError(), {"passwordNeeded": False}])
    monkeypatch.setattr("custom_components.kiosk_satellite_manager.install.ks_api_client.get_setup_status", status)
    monkeypatch.setattr(
        "custom_components.kiosk_satellite_manager.install.ks_api_client.probe_https",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("custom_components.kiosk_satellite_manager.install.asyncio.sleep", sleep)

    assert await _wait_for_setup_status(MagicMock(), "192.168.1.50") == ({"passwordNeeded": False}, None)
    assert status.await_count == 2
    sleep.assert_awaited_once()

    status.reset_mock(side_effect=True)
    status.side_effect = aiohttp.ClientConnectionError("refused")
    with pytest.raises(KsApiError, match="never became reachable: refused"):
        await _wait_for_setup_status(MagicMock(), "192.168.1.50")
    assert status.await_count == SYNC_STATUS_POLL_ATTEMPTS


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
            await install_and_launch(hass, client, session, device_model="portal_go")


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
            await install_and_launch(hass, client, session, device_model="portal_go")


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
            await install_and_launch(hass, client, session, host="192.168.1.50", device_model="portal_go")


# KSM-BEHAVE-041 (Phase 3, "permission convergence"): converge_permissions
# is exercised directly here, separate from install_and_launch's own tests
# above, since it has its own multi-surface postcondition contract.


def _converging_client():
    client = MagicMock()
    client.shell = AsyncMock(return_value="")
    client.granted_permissions = AsyncMock(return_value=set(PORTAL_PERMISSIONS))
    client.appop_mode = AsyncMock(return_value="allow")
    client.is_battery_exempt = AsyncMock(return_value=True)
    client.declared_bound_services = AsyncMock(return_value={})
    client.get_secure_setting = AsyncMock(return_value="")
    client.put_secure_setting = AsyncMock()
    return client


async def test_converge_permissions_fully_converged_when_everything_reads_back_granted():
    client = _converging_client()
    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)
    assert result.fully_converged is True
    assert result.denied_permissions == []
    assert result.denied_appops == []
    assert result.accessibility == "not_applicable"
    assert result.notification_listener == "not_applicable"


async def test_converge_permissions_detects_a_permission_the_device_refused():
    """KSM-TEST-044 (negative control): a permission grant that doesn't
    stick must show up as denied, not silently disappear -- confirms the
    readback, not just `pm grant`'s exit code, drives the result."""
    client = _converging_client()
    refused = PORTAL_PERMISSIONS[0]
    still_granted = set(PORTAL_PERMISSIONS) - {refused}
    client.granted_permissions = AsyncMock(return_value=still_granted)

    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)

    assert result.denied_permissions == [refused]
    assert refused not in result.granted_permissions
    assert result.fully_converged is False


async def test_converge_permissions_detects_an_appop_the_device_refused():
    client = _converging_client()
    client.appop_mode = AsyncMock(side_effect=["allow", "ignore", "allow"])

    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)

    assert result.denied_appops == [PORTAL_APPOPS[1]]
    assert result.fully_converged is False


async def test_converge_permissions_detects_battery_exemption_not_applied():
    client = _converging_client()
    client.is_battery_exempt = AsyncMock(return_value=False)

    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)

    assert result.battery_exempt is False
    assert result.fully_converged is False


async def test_converge_permissions_accessibility_not_applicable_when_ks_declares_no_service():
    client = _converging_client()
    client.declared_bound_services = AsyncMock(return_value={})

    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)

    assert result.accessibility == "not_applicable"
    client.put_secure_setting.assert_not_called()


async def test_converge_permissions_accessibility_granted_and_preserves_existing_services():
    """The additive settings-put must never drop an already-enabled OEM
    accessibility service -- docs/developer/android-support/app-lifecycle.md
    explicitly warns that overwriting the whole string disables every other
    service on the device."""
    client = _converging_client()
    ks_component = "me.jxl.kiosk_satellite/.KioskAccessibilityService"
    client.declared_bound_services = AsyncMock(
        return_value={"android.permission.BIND_ACCESSIBILITY_SERVICE": ks_component}
    )
    existing = "com.facebook.aloha.system.device/.accessibility.KeyEventAccessibilityService"
    settings_state = {"enabled_accessibility_services": existing, "accessibility_enabled": "1"}

    async def fake_get(key):
        return settings_state[key]

    async def fake_put(key, value):
        settings_state[key] = value

    client.get_secure_setting = AsyncMock(side_effect=fake_get)
    client.put_secure_setting = AsyncMock(side_effect=fake_put)

    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)

    assert result.accessibility == "granted"
    final_parts = set(settings_state["enabled_accessibility_services"].split(":"))
    assert existing in final_parts
    assert ks_component in final_parts


async def test_converge_permissions_accessibility_needs_user_interaction_when_write_does_not_stick():
    """Some OEM builds gate accessibility enablement behind an on-device
    tap even after the ADB write lands -- the issue's acceptance text says
    to record that, never claim success."""
    client = _converging_client()
    ks_component = "me.jxl.kiosk_satellite/.KioskAccessibilityService"
    client.declared_bound_services = AsyncMock(
        return_value={"android.permission.BIND_ACCESSIBILITY_SERVICE": ks_component}
    )
    # The write is accepted but the readback never reflects it -- simulates
    # an OEM skin silently rejecting the settings write.
    client.get_secure_setting = AsyncMock(return_value="")
    client.put_secure_setting = AsyncMock()

    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)

    assert result.accessibility == "needs_user_interaction"
    assert result.fully_converged is False


async def test_converge_permissions_notification_listener_not_applicable_for_ks():
    """Live-confirmed against the Test Portal (2026-09-20): Kiosk
    Satellite declares no NotificationListenerService component."""
    client = _converging_client()
    client.declared_bound_services = AsyncMock(
        return_value={"android.permission.BIND_ACCESSIBILITY_SERVICE": "me.jxl.kiosk_satellite/.KioskAccessibilityService"}
    )
    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)
    assert result.notification_listener == "not_applicable"


# KSM-BEHAVE-046 (Phase 5, "functional verification"): verify_functional_capabilities
# is exercised directly here, against a PermissionConvergenceResult built by hand
# rather than a real converge_permissions() run, since its own contract (Phase 3)
# already has full coverage above.

_MIC = "android.permission.RECORD_AUDIO"
_CAMERA = "android.permission.CAMERA"
_BT_SCAN = "android.permission.BLUETOOTH_SCAN"
_BT_CONNECT = "android.permission.BLUETOOTH_CONNECT"


def _convergence(
    granted: list[str] | None = None, denied: list[str] | None = None
) -> PermissionConvergenceResult:
    return PermissionConvergenceResult(
        granted_permissions=granted or [],
        denied_permissions=denied or [],
        granted_appops=[],
        denied_appops=[],
        battery_exempt=True,
        accessibility="not_applicable",
        notification_listener="not_applicable",
    )


async def test_verify_functional_capabilities_ok_when_granted_and_radio_on():
    client = MagicMock()
    client.bluetooth_enabled = AsyncMock(return_value=True)
    convergence = _convergence(granted=[_MIC, _CAMERA])

    result = await verify_functional_capabilities(client, convergence)

    assert result.microphone == "ok"
    assert result.camera == "ok"
    assert result.bluetooth == "ok"
    assert result.fully_verified is True


async def test_verify_functional_capabilities_detects_denied_microphone():
    """KSM-TEST-052 (negative control): a denied RECORD_AUDIO must surface
    as a failed microphone check, not be silently reported ok."""
    client = MagicMock()
    client.bluetooth_enabled = AsyncMock(return_value=True)
    convergence = _convergence(granted=[_CAMERA], denied=[_MIC])

    result = await verify_functional_capabilities(client, convergence)

    assert result.microphone == "permission_denied"
    assert result.fully_verified is False


async def test_verify_functional_capabilities_detects_denied_camera():
    """KSM-TEST-052 (negative control): same as above for CAMERA."""
    client = MagicMock()
    client.bluetooth_enabled = AsyncMock(return_value=True)
    convergence = _convergence(granted=[_MIC], denied=[_CAMERA])

    result = await verify_functional_capabilities(client, convergence)

    assert result.camera == "permission_denied"
    assert result.fully_verified is False


async def test_verify_functional_capabilities_detects_denied_bluetooth_permission():
    """KSM-TEST-052 (negative control): on SDK >= 31, a requested-but-denied
    BLUETOOTH_CONNECT must surface as denied even though the radio itself
    is on -- a granted radio doesn't imply the app can actually use it."""
    client = MagicMock()
    client.bluetooth_enabled = AsyncMock(return_value=True)
    convergence = _convergence(granted=[_MIC, _CAMERA, _BT_SCAN], denied=[_BT_CONNECT])

    result = await verify_functional_capabilities(client, convergence)

    assert result.bluetooth == "permission_denied"
    client.bluetooth_enabled.assert_not_awaited()
    assert result.fully_verified is False


async def test_verify_functional_capabilities_detects_bluetooth_radio_off():
    """KSM-TEST-053 (negative control): live-confirmed against the Test
    Portal (SDK 29) that no BLUETOOTH_SCAN/CONNECT permission is even
    requested pre-SDK-31 (install_recipes.permissions_for_sdk) -- so the
    device's own radio state is the only meaningful Bluetooth signal here,
    and a disabled radio must be detected rather than reported ok just
    because there was no permission to deny."""
    client = MagicMock()
    client.bluetooth_enabled = AsyncMock(return_value=False)
    convergence = _convergence(granted=[_MIC, _CAMERA])

    result = await verify_functional_capabilities(client, convergence)

    assert result.bluetooth == "adapter_disabled"
    client.bluetooth_enabled.assert_awaited_once()
    assert result.fully_verified is False


async def test_verify_functional_capabilities_bluetooth_ok_when_all_permissions_granted_and_radio_on():
    """SDK >= 31 positive path: both BLUETOOTH_SCAN/CONNECT granted and the
    radio on together are required for "ok" -- neither alone is sufficient."""
    client = MagicMock()
    client.bluetooth_enabled = AsyncMock(return_value=True)
    convergence = _convergence(granted=[_MIC, _CAMERA, _BT_SCAN, _BT_CONNECT])

    result = await verify_functional_capabilities(client, convergence)

    assert result.bluetooth == "ok"
    client.bluetooth_enabled.assert_awaited_once()


async def test_install_and_launch_runs_functional_verification_after_permission_convergence():
    """KSM-TEST-054: install_and_launch's own end-to-end flow must actually
    invoke Phase 5's functional verification, not just leave it as dead
    code -- confirms the wiring, not only the standalone function."""
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        await install_and_launch(hass, client, session, device_model="portal_go")

    client.bluetooth_enabled.assert_awaited_once()
