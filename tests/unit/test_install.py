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
from custom_components.kiosk_satellite_manager.install_recipes import INSTALL_RECIPES, get_recipe
from custom_components.kiosk_satellite_manager.install import (
    KsInstallVerificationFailed,
    PermissionConvergenceResult,
    PrivateDnsChangeFailed,
    check_dashboard_dns,
    disable_private_dns,
    restore_private_dns,
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
PORTAL_RECIPE = get_recipe("meta_portal_android10_local_dns")
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
        self.data: dict = {}

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
    # the meta_portal permission/appops set -- individual
    # convergence tests override these to exercise the
    # partial/needs-user-interaction paths.
    client.granted_permissions = AsyncMock(return_value=set(PORTAL_PERMISSIONS))
    client.appop_mode = AsyncMock(return_value="allow")
    client.is_battery_exempt = AsyncMock(return_value=True)
    client.declared_bound_services = AsyncMock(return_value={})
    client.get_secure_setting = AsyncMock(return_value="")
    client.put_secure_setting = AsyncMock()
    # KSM-BEHAVE-152: Private DNS already off, so no install changes it
    # unless a test says otherwise. KSM-BEHAVE-153: an unreadable resolver
    # answer skips the dashboard DNS check.
    client.get_global_setting = AsyncMock(return_value="off")
    client.put_global_setting = AsyncMock()
    client.delete_global_setting = AsyncMock()
    client.resolve_host = AsyncMock(return_value=None)
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


_APK_CACHE = "custom_components.kiosk_satellite_manager.apk_cache."


@pytest.fixture(autouse=True)
def apk_cache_dir(monkeypatch, tmp_path):
    """Existing install-flow tests use arbitrary bytes, not signed APK files.
    KSM-BEHAVE-107: the ADB path goes through KSM's APK cache, kept here in
    a per-test directory."""
    monkeypatch.setattr(_APK_CACHE + "verify_ks_apk_signer", MagicMock())
    monkeypatch.setattr(_APK_CACHE + "cache_root", lambda hass: tmp_path / "apks")
    return tmp_path / "apks"


async def test_install_and_launch_pushes_the_cached_apk_and_keeps_it(apk_cache_dir):
    """[KSM-TEST-207] KSM-BEHAVE-107: Install/Reinstall downloads into the
    same cache the API update path uploads from, pushes that file, and
    leaves it cached -- a second press downloads nothing."""
    session = _fake_session()
    target = apk_cache_dir / _TARGET_VERSION / "kiosk-satellite-2026.9.99.apk"
    for _ in range(2):
        client = _fake_client()
        client.installed_version = AsyncMock(side_effect=[None, _TARGET_VERSION])
        with patch(
            "custom_components.kiosk_satellite_manager.install.latest_release",
            new=AsyncMock(
                return_value=(
                    "https://example.invalid/dl/kiosk-satellite-2026.9.99.apk",
                    _TARGET_VERSION,
                )
            ),
        ):
            await install_and_launch(_FakeHass(), client, session, device_model="portal_go")
        client.push.assert_awaited_once_with(str(target), client.install_apk.await_args.args[0])
        # #74: the device's own ABI list picks the split.
        assert "ro.product.cpu.abilist" in [c.args[0] for c in client.getprop.await_args_list]

    assert target.read_bytes() == b"fake-apk-bytes"
    apk_gets = [c for c in session.get.call_args_list if c.args[0].endswith(".apk")]
    assert len(apk_gets) == 1


def _release_check(hass, version=_TARGET_VERSION):
    """A shared release check whose last successful run saw `version`."""
    from custom_components.kiosk_satellite_manager.const import RELEASE_COORDINATOR_KEY
    from custom_components.kiosk_satellite_manager.ks_api import ReleaseInfo

    url = f"https://example.invalid/dl/kiosk-satellite-{version}.arm64-v8a.apk"
    hass.data[RELEASE_COORDINATOR_KEY] = SimpleNamespace(
        data=ReleaseInfo(version, None, None, ((url.rsplit("/", 1)[-1], url),))
    )


def _rate_limited_session():
    """_fake_session, but the GitHub releases API answers 403 rate limited."""
    session = _fake_session()
    serve = session.get.side_effect

    def _get(url, **kwargs):
        if "api.github.com" in url:
            resp = MagicMock()
            resp.raise_for_status = MagicMock(
                side_effect=aiohttp.ClientResponseError(
                    MagicMock(), (), status=403, message="rate limit exceeded"
                )
            )
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=resp)
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm
        return serve(url, **kwargs)

    session.get = MagicMock(side_effect=_get)
    return session


async def test_install_uses_the_last_successful_release_check(apk_cache_dir, monkeypatch):
    """[KSM-TEST-293] KSM-BEHAVE-147 (#107): with the release check holding a
    result, Install pushes that release's split and asks GitHub's API
    nothing -- even while GitHub is rate limiting. Negative case: with no
    successful check, the live lookup is the target."""
    hass = _FakeHass()
    _release_check(hass)
    session = _rate_limited_session()
    monkeypatch.setattr(_APK_CACHE + "async_get_clientsession", lambda hass: session)
    client = _fake_client()
    lookup = AsyncMock(side_effect=AssertionError("live release lookup"))
    with patch("custom_components.kiosk_satellite_manager.install.latest_release", new=lookup):
        await install_and_launch(hass, client, session, device_model="portal_go")

    lookup.assert_not_awaited()
    target = apk_cache_dir / _TARGET_VERSION / f"kiosk-satellite-{_TARGET_VERSION}.arm64-v8a.apk"
    client.push.assert_awaited_once_with(str(target), client.install_apk.await_args.args[0])
    assert not [c for c in session.get.call_args_list if "api.github.com" in c.args[0]]

    client = _fake_client()
    lookup = AsyncMock(return_value=("https://example.invalid/dl/ks.apk", _TARGET_VERSION))
    with patch("custom_components.kiosk_satellite_manager.install.latest_release", new=lookup):
        await install_and_launch(_FakeHass(), client, _fake_session(), device_model="portal_go")
    lookup.assert_awaited_once()


async def test_rate_limited_release_lookup_fails_as_a_home_assistant_error():
    """[KSM-TEST-294] KSM-BEHAVE-147 (#107): no successful release check and
    GitHub answering 403 -> a HomeAssistantError naming the release lookup
    (the service call's 400, not an unhandled 500), before the device is
    touched."""
    from homeassistant.exceptions import HomeAssistantError

    client = _fake_client()
    with pytest.raises(HomeAssistantError, match="release lookup"):
        await install_and_launch(
            _FakeHass(), client, _rate_limited_session(), device_model="portal_go"
        )
    client.push.assert_not_awaited()
    client.install_apk.assert_not_awaited()
    client.shell.assert_not_awaited()


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


async def test_install_never_changes_native_or_android_home():
    """[KSM-TEST-257] Install leaves KS and Android Home state to KS."""
    for model in ("portal_go", "portal_mini", "portal_gen1", "portal_gen2", "portal_tv"):
        client = _fake_client()
        with patch(
            "custom_components.kiosk_satellite_manager.install.latest_release",
            new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
        ):
            await install_and_launch(
                _FakeHass(), client, _fake_session(), device_model=model
            )
        commands = [call.args[0] for call in client.shell.await_args_list]
        assert all("ks.provision" not in command for command in commands)
        assert all("set-home-activity" not in command for command in commands)


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


async def test_install_and_launch_uses_the_assigned_recipe_for_start_url():
    """[KSM-TEST-257] Portal TV shares the Portal start path without Home control."""
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
        mock_api.check_ha_connection = AsyncMock(return_value=(True, None))

        await install_and_launch(
            hass,
            client,
            session,
            host="192.168.1.50",
            device_name="Portal TV",
            password="1newpass",
            device_model="portal_tv",
        )

        settings_payload = mock_api.patch_settings.await_args[0][3]
        assert settings_payload["browser.start_url"] == "http://192.168.1.2:8123/portal"
        assert "home.enabled" not in settings_payload



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


async def test_install_and_launch_syncs_password_and_name_on_first_run(caplog):
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
        mock_api.check_ha_connection = AsyncMock(return_value=(True, None))

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
    assert "HA connection check failed" not in caplog.text


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("certificate rejected; ks-token; minted-ha-token; hunter22", "certificate rejected"),
        (None, "no error detail supplied"),
    ],
)
async def test_failed_ha_connection_check_preserves_native_home_and_reports_warning(caplog, error, expected):
    """[KSM-TEST-257] A failed HA check does not trigger Home repair."""
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()
    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch("custom_components.kiosk_satellite_manager.install.ks_api_client") as mock_api, patch(
        "custom_components.kiosk_satellite_manager.install.get_url",
        return_value="http://192.168.1.2:8123",
    ):
        mock_api.probe_https = AsyncMock(return_value=None)
        mock_api.get_setup_status = AsyncMock(return_value={"passwordNeeded": True})
        mock_api.setup_password = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=(False, error))
        await install_and_launch(
            hass, client, session, host="192.168.1.50", device_name="Kitchen",
            password="hunter22", device_model="portal_go",
        )
    assert "HA connection check failed" in caplog.text
    assert "192.168.1.50" in caplog.text
    assert expected in caplog.text
    assert "ks-token" not in caplog.text
    assert "minted-ha-token" not in caplog.text
    assert "hunter22" not in caplog.text
    assert "home.enabled" not in mock_api.patch_settings.await_args.args[3]
    assert all("set-home-activity" not in call.args[0] for call in client.shell.await_args_list)


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
        mock_api.check_ha_connection = AsyncMock(return_value=(True, None))

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
        mock_api.check_ha_connection = AsyncMock(return_value=(True, None))

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
        mock_api.check_ha_connection = AsyncMock(return_value=(True, None))

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

    # portal_go grants the same 7 runtime permissions as the generic list:
    # KS never declared WRITE_SECURE_SETTINGS, so the Portal recipe no longer
    # grants it (KSM-BEHAVE-151). Package verification remains enabled
    # (KSM-BEHAVE-062), and the Portal recipe issues no launcher mutation
    # (KSM-BEHAVE-069).
    assert client.shell.await_count == 14
    grants = [c.args[0] for c in client.shell.await_args_list if c.args[0].startswith("pm grant ")]
    assert len(grants) == 7
    assert not any("WRITE_SECURE_SETTINGS" in g for g in grants)


async def test_install_and_launch_update_settings_reports_rejected_sync():
    """[KSM-TEST-347] A failed settings update must reach the onboarding caller."""
    hass = _FakeHass()
    client = _fake_client()
    session = _fake_session()

    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api:
        mock_api.probe_https = AsyncMock(return_value=None)
        mock_api.get_setup_status = AsyncMock(side_effect=KsApiError("password rejected"))

        with pytest.raises(KsApiError, match="password rejected"):
            await install_and_launch(
                hass, client, session, host="192.168.1.50", device_name="Kitchen",
                password="existing-password", device_model="portal_go",
                fail_on_sync_error=True,
            )


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
        mock_api.check_ha_connection = AsyncMock(return_value=(True, None))

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
        _APK_CACHE + "verify_ks_apk_signer",
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
    client.getprop = AsyncMock(side_effect=[OSError("probe failed")])
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
    """An older KS build without the service needs no notification grant."""
    client = _converging_client()
    client.declared_bound_services = AsyncMock(
        return_value={"android.permission.BIND_ACCESSIBILITY_SERVICE": "me.jxl.kiosk_satellite/.KioskAccessibilityService"}
    )
    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)
    assert result.notification_listener == "not_applicable"


@pytest.mark.parametrize("recipe", INSTALL_RECIPES, ids=lambda r: r.recipe_key)
async def test_KSM_TEST_270_notification_access_converges_for_every_recipe(recipe):
    client = _converging_client()
    client.granted_permissions = AsyncMock(return_value=set(recipe.permissions_for_sdk(34)))
    component = "me.jxl.kiosk_satellite/.MediaSessionListener"
    client.declared_bound_services = AsyncMock(return_value={
        "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE": component,
    })
    client.is_notification_listener_bound = AsyncMock(side_effect=[False, True])
    result = await converge_permissions(client, sdk=34, recipe=recipe)
    assert result.notification_listener == "granted"
    assert any(
        call.args[0] == f"cmd notification allow_listener {component}"
        for call in client.shell.await_args_list
    )
    client.put_secure_setting.assert_not_awaited()


async def test_notification_listener_grant_quotes_device_reported_component():
    """[KSM-TEST-287] The listener component comes from the device's dumpsys
    output; it reaches `cmd notification allow_listener` shell-quoted."""
    client = _converging_client()
    component = "me.jxl.kiosk_satellite/.L;reboot"
    client.declared_bound_services = AsyncMock(return_value={
        "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE": component,
    })
    client.is_notification_listener_bound = AsyncMock(side_effect=[False, True])
    await converge_permissions(client, sdk=34, recipe=PORTAL_RECIPE)
    commands = [call.args[0] for call in client.shell.await_args_list]
    assert "cmd notification allow_listener 'me.jxl.kiosk_satellite/.L;reboot'" in commands
    assert not any(c.endswith("/.L;reboot") for c in commands)


async def test_KSM_TEST_273_secure_setting_alone_cannot_claim_notification_access():
    client = _converging_client()
    component = "me.jxl.kiosk_satellite/.MediaSessionListener"
    client.declared_bound_services = AsyncMock(return_value={
        "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE": component,
    })
    client.get_secure_setting = AsyncMock(return_value=component)
    client.is_notification_listener_bound = AsyncMock(return_value=False)
    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)
    assert result.notification_listener == "needs_user_interaction"
    assert result.fully_converged is False


async def test_KSM_TEST_273_bound_notification_listener_is_left_alone():
    client = _converging_client()
    component = "me.jxl.kiosk_satellite/.MediaSessionListener"
    client.declared_bound_services = AsyncMock(return_value={
        "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE": component,
    })
    client.is_notification_listener_bound = AsyncMock(return_value=True)
    result = await converge_permissions(client, sdk=29, recipe=PORTAL_RECIPE)
    assert result.notification_listener == "granted"
    assert not any("cmd notification allow_listener" in c.args[0] for c in client.shell.await_args_list)
    client.put_secure_setting.assert_not_awaited()


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


async def test_install_and_launch_pushes_the_pinned_cached_apk(apk_cache_dir):
    """[KSM-TEST-221] #72: with an Install version pinned, ADB Install pushes
    that cached file and never asks GitHub for the latest release."""
    pinned = apk_cache_dir / "2026.9.86" / "kiosk-satellite-2026.9.86.apk"
    pinned.parent.mkdir(parents=True)
    pinned.write_bytes(b"pinned")
    session = _fake_session()
    client = _fake_client()
    client.installed_version = AsyncMock(side_effect=[None, "2026.9.86"])
    latest = AsyncMock()
    with patch(
        "custom_components.kiosk_satellite_manager.helpers.pinned_version",
        return_value="2026.9.86",
    ), patch("custom_components.kiosk_satellite_manager.install.latest_release", new=latest):
        await install_and_launch(_FakeHass(), client, session, device_model="portal_go")
    latest.assert_not_awaited()
    client.push.assert_awaited_once_with(str(pinned), client.install_apk.await_args.args[0])
    assert not [c for c in session.get.call_args_list if c.args[0].endswith(".apk")]


async def test_install_and_launch_asks_for_the_device_split(apk_cache_dir):
    """[KSM-TEST-224] #74: ADB Install reads ro.product.cpu.abilist and asks
    for the latest release's split for those ABIs. Negative: an empty
    abilist falls back to ro.product.cpu.abi."""
    for props, abis in (
        ({"ro.product.cpu.abilist": "arm64-v8a,armeabi-v7a,armeabi"}, ["arm64-v8a", "armeabi-v7a", "armeabi"]),
        ({"ro.product.cpu.abilist": "", "ro.product.cpu.abi": "armeabi-v7a"}, ["armeabi-v7a"]),
    ):
        client = _fake_client()
        client.getprop = AsyncMock(side_effect=lambda name, p=props: p.get(name, "29"))
        client.installed_version = AsyncMock(side_effect=[None, _TARGET_VERSION])
        latest = AsyncMock(return_value=(
            "https://example.invalid/dl/kiosk-satellite-2026.9.99.x.apk", _TARGET_VERSION,
        ))
        with patch("custom_components.kiosk_satellite_manager.install.latest_release", new=latest):
            await install_and_launch(_FakeHass(), client, _fake_session(), device_model="portal_go")
        assert list(latest.await_args.args[1]) == abis


async def test_install_and_launch_reuses_a_cached_universal_apk(apk_cache_dir):
    """[KSM-TEST-224] ADB Install on Latest pushes a universal APK already
    cached for the target version and downloads nothing. Negative control:
    with nothing cached it downloads the device's split."""
    universal = apk_cache_dir / _TARGET_VERSION / "kiosk-satellite-2026.9.99.apk"
    universal.parent.mkdir(parents=True)
    universal.write_bytes(b"universal")
    for cached in (True, False):
        if not cached:
            universal.unlink()
        session = _fake_session()
        client = _fake_client()
        client.installed_version = AsyncMock(side_effect=[None, _TARGET_VERSION])
        latest = AsyncMock(return_value=(
            "https://example.invalid/dl/kiosk-satellite-2026.9.99.arm64-v8a.apk", _TARGET_VERSION,
        ))
        with patch("custom_components.kiosk_satellite_manager.install.latest_release", new=latest):
            await install_and_launch(_FakeHass(), client, session, device_model="portal_go")
        apk_gets = [c for c in session.get.call_args_list if c.args[0].endswith(".apk")]
        pushed = client.push.await_args.args[0]
        if cached:
            assert (pushed, apk_gets) == (str(universal), [])
        else:
            assert pushed.endswith("kiosk-satellite-2026.9.99.arm64-v8a.apk") and len(apk_gets) == 1


_KS_HOME = "me.jxl.kiosk_satellite/.HomeAlias"
_META_HOME = "com.facebook.alohaapps.launcher/com.facebook.aloha.app.home.touch.HomeActivity"


async def _run_install_for_launcher(*, replace_launcher, resolver, device_model="portal_go"):
    hass = _FakeHass()
    client = _fake_client()
    client.resolved_home_activity = AsyncMock(return_value=resolver)
    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ), patch(
        "custom_components.kiosk_satellite_manager.install.ks_api_client"
    ) as mock_api, patch(
        "custom_components.kiosk_satellite_manager.install.get_url",
        return_value="http://192.168.1.2:8123",
    ), patch(
        "custom_components.kiosk_satellite_manager.install.persistent_notification"
    ) as notify, patch(
        "custom_components.kiosk_satellite_manager.install.asyncio.sleep", new=AsyncMock()
    ):
        mock_api.probe_https = AsyncMock(return_value=None)
        mock_api.get_setup_status = AsyncMock(
            return_value={"passwordNeeded": False, "deviceName": "Kitchen"}
        )
        mock_api.login = AsyncMock(return_value="ks-token")
        mock_api.patch_settings = AsyncMock(return_value={})
        mock_api.check_ha_connection = AsyncMock(return_value=(True, None))
        result = await install_and_launch(
            hass, client, _fake_session(),
            host="192.168.1.50", device_name="Kitchen", password="hunter22",
            device_model=device_model, ha_token="tok", ha_url="https://ha.example.test",
            replace_launcher=replace_launcher,
        )
    payloads = [c.args[3] for c in mock_api.patch_settings.await_args_list]
    return client, payloads, notify, result


async def test_KSM_TEST_279_replace_launcher_enables_selects_and_reads_back():
    """[KSM-TEST-279] Option on: home.enabled goes in the sync patch, the alias
    is selected, and the resolver readback decides success."""
    client, payloads, notify, _ = await _run_install_for_launcher(
        replace_launcher=True, resolver=_KS_HOME
    )
    assert any(p.get("home.enabled") is True for p in payloads)
    client.select_ks_home.assert_awaited()
    notify.async_create.assert_not_called()


async def test_KSM_TEST_279_resolver_miss_is_reported_and_install_still_syncs():
    """[KSM-TEST-279] Negative: set-home-activity ran but the resolver still
    names Meta's launcher -> a notification, and the HA sync is unaffected."""
    client, payloads, notify, result = await _run_install_for_launcher(
        replace_launcher=True, resolver=_META_HOME
    )
    client.select_ks_home.assert_awaited()
    notify.async_create.assert_called_once()
    assert "192.168.1.50" in notify.async_create.call_args.kwargs["message"]
    assert result == TokenCredential("tok", None, owned=False)
    assert any("ha.url" in p for p in payloads)


async def test_KSM_TEST_280_replace_launcher_off_touches_no_home_state():
    """[KSM-TEST-280] Option off: no set-home-activity, no home.enabled."""
    client, payloads, notify, _ = await _run_install_for_launcher(
        replace_launcher=False, resolver=_META_HOME
    )
    client.select_ks_home.assert_not_awaited()
    client.resolved_home_activity.assert_not_awaited()
    assert all("home.enabled" not in p for p in payloads)
    notify.async_create.assert_not_called()


def test_KSM_TEST_281_default_follows_recipe_and_explicit_value_wins():
    """[KSM-TEST-281] Unset: Portal recipes on, everything else off."""
    from custom_components.kiosk_satellite_manager.install import launcher_replacement_wanted

    for key in ("meta_portal_android10_local_dns", "meta_portal_android9_local_dns", "meta_portal_tv_local_dns"):
        assert launcher_replacement_wanted({}, get_recipe(key)) is True
    for key in ("onn_4k_pro_android14", "android_tv"):
        assert launcher_replacement_wanted({}, get_recipe(key)) is False
    assert launcher_replacement_wanted({"replace_launcher": False}, get_recipe("meta_portal_android10_local_dns")) is False
    assert launcher_replacement_wanted({"replace_launcher": True}, get_recipe("android_tv")) is True


# KSM-BEHAVE-152 (#121): Portal OS adds 1.1.1.1 to the DHCP DNS list, and
# Android's default opportunistic Private DNS validates and prefers it, so the
# Portal resolves a split-horizon dashboard host to its public address.


def _dns_client(*readings):
    client = _fake_client()
    client.get_global_setting = AsyncMock(side_effect=list(readings))
    return client


@pytest.mark.parametrize("prior", ["", "opportunistic"])
async def test_KSM_TEST_305_portal_install_turns_private_dns_off_and_reports_prior(prior):
    client = _dns_client(prior, "off")
    reported = []
    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        await install_and_launch(
            _FakeHass(), client, _fake_session(), device_model="portal_go",
            on_private_dns_disabled=reported.append,
        )
    client.put_global_setting.assert_awaited_once_with("private_dns_mode", "off")
    assert reported == [prior]


@pytest.mark.parametrize("current", ["off", "hostname"])
async def test_KSM_TEST_305_private_dns_left_alone_when_off_or_user_chosen(current, caplog):
    """A strict "hostname" Private DNS is the user's explicit choice; KSM
    never overrides it, and an already-off device has nothing to record."""
    client = _dns_client(current)
    assert await disable_private_dns(client, "192.168.40.224") is None
    client.put_global_setting.assert_not_awaited()
    client.delete_global_setting.assert_not_awaited()
    if current == "hostname":
        assert "Private DNS" in caplog.text


async def test_KSM_TEST_305_private_dns_change_is_read_back():
    client = _dns_client("", "opportunistic")
    with pytest.raises(PrivateDnsChangeFailed):
        await disable_private_dns(client, "192.168.40.224")


def _never_reported(prior):
    raise AssertionError(f"non-Portal install reported Private DNS prior {prior!r}")


async def test_KSM_TEST_305_non_portal_install_never_reads_or_changes_private_dns():
    client = _fake_client()
    with patch(
        "custom_components.kiosk_satellite_manager.install.latest_release",
        new=AsyncMock(return_value=("https://example.invalid/ks.apk", _TARGET_VERSION)),
    ):
        await install_and_launch(
            _FakeHass(), client, _fake_session(), device_model="onn_4k_pro_android14",
            on_private_dns_disabled=_never_reported,
        )
    client.get_global_setting.assert_not_awaited()
    client.put_global_setting.assert_not_awaited()


@pytest.mark.parametrize(
    ("prior", "after", "method", "args"),
    [
        ("", "", "delete_global_setting", ("private_dns_mode",)),
        ("opportunistic", "opportunistic", "put_global_setting", ("private_dns_mode", "opportunistic")),
    ],
)
async def test_KSM_TEST_305_restore_puts_back_the_recorded_mode(prior, after, method, args):
    client = _dns_client("off", after)
    assert await restore_private_dns(client, prior) is True
    getattr(client, method).assert_awaited_once_with(*args)


async def test_KSM_TEST_305_restore_leaves_a_mode_changed_since_install():
    client = _dns_client("hostname")
    assert await restore_private_dns(client, "") is False
    client.put_global_setting.assert_not_awaited()
    client.delete_global_setting.assert_not_awaited()


async def test_KSM_TEST_305_restore_is_read_back():
    client = _dns_client("off", "off")
    with pytest.raises(PrivateDnsChangeFailed):
        await restore_private_dns(client, "opportunistic")


# KSM-BEHAVE-153: every device, whatever its recipe.

_HA_ADDRESSES = "custom_components.kiosk_satellite_manager.install._ha_addresses"


@pytest.mark.parametrize(
    ("device", "ha", "mismatch"),
    [
        ("99.1.33.71", {"192.168.40.115"}, True),
        ("", {"192.168.40.115"}, True),
        ("192.168.40.115", {"192.168.40.115", "192.168.90.2"}, False),
        # Live dev HA (#121): multi-homed, it answers on the device's VLAN.
        ("192.168.40.196", {"192.168.90.106"}, False),
        # A public dashboard host behind a CDN answers differently everywhere.
        ("104.16.1.1", {"104.16.2.2"}, False),
        ("99.1.33.71", {"192.168.40.115", "104.16.2.2"}, False),
    ],
)
async def test_KSM_TEST_306_dashboard_dns_compares_device_and_ha(device, ha, mismatch):
    client = _fake_client()
    client.resolve_host = AsyncMock(return_value=device)
    with patch(_HA_ADDRESSES, return_value=ha):
        check = await check_dashboard_dns(_FakeHass(), client, "https://ha.cfoxga.com:443/")
    client.resolve_host.assert_awaited_once_with("ha.cfoxga.com")
    assert check.hostname == "ha.cfoxga.com"
    assert check.device_address == device
    assert check.mismatch is mismatch


@pytest.mark.parametrize(
    ("url", "device", "ha"),
    [
        ("https://192.168.40.115:8123", "192.168.40.115", {"192.168.40.115"}),
        ("https://ha.cfoxga.com", None, {"192.168.40.115"}),
        ("https://ha.cfoxga.com", "99.1.33.71", set()),
        ("https://ha.cfoxga.com", "99.1.33.71", {"127.0.0.1"}),
        ("not a url", "99.1.33.71", {"192.168.40.115"}),
    ],
)
async def test_KSM_TEST_306_dashboard_dns_skips_when_it_cannot_compare(url, device, ha):
    """An IP-literal URL needs no DNS; an unreadable device answer or an HA
    with no usable address of its own is no evidence of a mismatch."""
    client = _fake_client()
    client.resolve_host = AsyncMock(return_value=device)
    with patch(_HA_ADDRESSES, return_value=ha):
        assert await check_dashboard_dns(_FakeHass(), client, url) is None


def test_KSM_TEST_306_ha_addresses_reads_the_ha_resolver():
    """HA's answer is every IPv4 address getaddrinfo returns; a name HA cannot
    resolve is an empty set, never an exception."""
    import socket

    from custom_components.kiosk_satellite_manager import install

    infos = [(socket.AF_INET, 0, 0, "", ("192.168.40.115", 0)),
             (socket.AF_INET, 0, 0, "", ("192.168.90.2", 0)),
             (socket.AF_INET, 0, 0, "", ("192.168.40.115", 0))]
    with patch.object(install.socket, "getaddrinfo", return_value=infos) as lookup:
        assert install._ha_addresses("ha.cfoxga.com") == {"192.168.40.115", "192.168.90.2"}
    lookup.assert_called_once_with("ha.cfoxga.com", None, socket.AF_INET)
    with patch.object(install.socket, "getaddrinfo", side_effect=socket.gaierror):
        assert install._ha_addresses("nowhere.invalid") == set()
