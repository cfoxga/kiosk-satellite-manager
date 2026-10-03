"""Install button integration test (KSM-BEHAVE-001, extended by
KSM-BEHAVE-007/008). AdbClient and the GitHub APK lookup are mocked at the
boundary -- the live ADB push/install path and the live GitHub releases API
shape are each verified separately (see docs/SPEC/provisioning.md). This
test proves the button wires the shared install_and_launch sequence
together, flags the coordinator "installing" for its duration, and refreshes
the version sensor afterward.
"""
from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import EntityCategory
from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager.adb_client import AdbConnectFailed
from custom_components.kiosk_satellite_manager.const import (
    CONF_AREA_ID,
    CONF_DEVICE_PROFILE,
    CONF_ENTRY_TYPE,
    CONF_HA_TOKEN,
    CONF_HA_REFRESH_TOKEN_ID,
    CONF_HA_TOKEN_OWNED,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PRIVATE_DNS_PRIOR,
    CONF_TLS_SPKI,
    CONF_TOKEN_MODE,
    DOMAIN,
    ENTRY_TYPE_MANAGER,
    INSTALL_LAUNCH_POLL_ATTEMPTS,
    TOKEN_MODE_AUTO,
)
from custom_components.kiosk_satellite_manager.credentials import TokenCredential
from custom_components.kiosk_satellite_manager.device_catalog import NoApprovedRecipe
from custom_components.kiosk_satellite_manager import fleet
from custom_components.kiosk_satellite_manager.button import KioskSatelliteInstallButton, async_install_entry
from custom_components.kiosk_satellite_manager.repairs import async_create_fix_flow
from custom_components.kiosk_satellite_manager.ks_api import ReleaseInfo

from .conftest import init_integration as _init_integration

_LOGIN = "custom_components.kiosk_satellite_manager.ks_update.ks_api_client.login"
_RUN_COMMAND = "custom_components.kiosk_satellite_manager.ks_update.ks_api_client.run_command"
_POLL_HEALTH = "custom_components.kiosk_satellite_manager.ks_update.fetch_health"


async def init_integration(hass, *, data=None, options=None):
    """Existing install tests use a device that already has an assigned Area."""
    area = ar.async_get(hass).async_get_area("ksm_test_area")
    if area is None:
        area = ar.async_get(hass).async_create("KSM Test Area")
    return await _init_integration(
        hass, data={CONF_AREA_ID: area.id, **(data or {})}, options=options
    )


@pytest.mark.parametrize("native_subentry", [False, True])
async def test_KSM_TEST_348_install_prompts_for_missing_area_before_adb(hass, native_subentry):
    area = ar.async_get(hass).async_create("Kitchen")
    data = {
        "host": "192.168.99.99", "port": 5555, "key_path": "/tmp/test-key",
        "area_id": None,
    }
    if native_subentry:
        parent = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: "fleet"})
        parent.add_to_hass(hass)
        hass.config_entries.async_add_subentry(parent, ConfigSubentry(
            data=MappingProxyType(data), subentry_id="kitchen-portal",
            subentry_type="device", title="Kitchen Portal", unique_id="kitchen-portal",
        ))
        entry = fleet.DeviceEntry(hass, parent, parent.subentries["kitchen-portal"])
        registry_owner = parent.entry_id
    else:
        entry = MockConfigEntry(domain=DOMAIN, title="Kitchen Portal", data=data)
        entry.add_to_hass(hass)
        registry_owner = entry.entry_id
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=registry_owner,
        **({"config_subentry_id": entry.subentry_id} if native_subentry else {}),
        identifiers={(DOMAIN, entry.entry_id)}, name=entry.title,
    )
    button = KioskSatelliteInstallButton(hass, entry)
    issue_id = f"area_required_{entry.entry_id}"
    with patch("custom_components.kiosk_satellite_manager.button.async_install_entry", new=AsyncMock()) as install:
        with pytest.raises(HomeAssistantError, match="Area"):
            await button.async_press()
        install.assert_not_awaited()
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        assert issue is not None and issue.is_fixable

        flow = await async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
        flow.hass = hass
        form = await flow.async_step_init()
        assert "area_id" in {field.schema for field in form["data_schema"].schema}
        invalid = await flow.async_step_area({"area_id": "missing-area"})
        assert invalid["errors"]["area_id"] == "area_not_found"
        assert dr.async_get(hass).async_get(device.id).area_id is None
        result = await flow.async_step_area({"area_id": area.id})
        assert result["type"] == "create_entry"
        assert dr.async_get(hass).async_get(device.id).area_id == area.id
        assert entry.data["area_id"] == area.id
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
        await button.async_press()
        install.assert_awaited_once()

        dr.async_get(hass).async_update_device(device.id, area_id=None)
        with pytest.raises(HomeAssistantError, match="Area"):
            await button.async_press()
        install.assert_awaited_once()


async def test_KSM_TEST_348_registry_area_allows_legacy_entry_to_install(hass):
    area = ar.async_get(hass).async_create("Kitchen")
    entry = MockConfigEntry(domain=DOMAIN, title="Kitchen Portal", data={
        "host": "192.168.99.99", "port": 5555, "key_path": "/tmp/test-key",
        CONF_AREA_ID: None,
    })
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, suggested_area=area.name,
    )
    with patch("custom_components.kiosk_satellite_manager.button.async_install_entry", new=AsyncMock()) as install:
        await KioskSatelliteInstallButton(hass, entry).async_press()
    install.assert_awaited_once()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"area_required_{entry.entry_id}") is None

@pytest.mark.parametrize("native_subentry", [False, True])
async def test_KSM_TEST_271_install_recovers_missing_profile_from_live_exact_facts(hass, native_subentry):
    """An old fleet subentry gets the same repair as an old direct entry."""
    data = {"host": "192.168.99.99", "port": 5555, "key_path": "/tmp/test-key",
            CONF_DEVICE_PROFILE: None}
    parent = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: "fleet"})
    parent.add_to_hass(hass)
    if native_subentry:
        hass.config_entries.async_add_subentry(parent, ConfigSubentry(
            data=MappingProxyType(data), subentry_id="gtv-ha", subentry_type="device",
            title="Theater GTV", unique_id="gtv-ha",
        ))
        entry = fleet.DeviceEntry(hass, parent, parent.subentries["gtv-ha"])
    else:
        entry = MockConfigEntry(domain=DOMAIN, data=data, title="Theater GTV")
        entry.add_to_hass(hass)
    props = {"ro.product.manufacturer": "onn", "ro.product.model": "onn 4K Pro Streaming Device",
             "ro.product.device": "jarvis2", "ro.build.version.sdk": "34"}
    with patch("custom_components.kiosk_satellite_manager.button.AdbClient") as cls, patch(
        "custom_components.kiosk_satellite_manager.button.install_and_launch",
        new=AsyncMock(return_value=None),
    ) as install, patch("custom_components.kiosk_satellite_manager.button.async_get_clientsession"):
        client = cls.return_value
        client.connect = AsyncMock()
        client.close = AsyncMock()
        client.getprop = AsyncMock(side_effect=lambda name: props.get(name, ""))
        await async_install_entry(hass, entry)
    assert install.await_args.kwargs["device_model"] == "onn_4k_pro_android14"
    assert entry.data[CONF_DEVICE_PROFILE] == "onn_4k_pro_android14"
    client.close.assert_awaited_once()


@pytest.mark.parametrize("unreadable", [False, True])
async def test_KSM_TEST_271_unknown_live_identity_remains_unprovisioned(hass, unreadable):
    entry = MockConfigEntry(domain=DOMAIN, title="Unknown TV", data={
        "host": "192.168.99.99", "port": 5555, "key_path": "/tmp/test-key",
        CONF_DEVICE_PROFILE: None,
    })
    entry.add_to_hass(hass)
    with patch("custom_components.kiosk_satellite_manager.button.AdbClient") as cls, patch(
        "custom_components.kiosk_satellite_manager.button.install_and_launch",
        new=AsyncMock(),
    ) as install, patch("custom_components.kiosk_satellite_manager.button.async_get_clientsession"):
        client = cls.return_value
        client.connect = AsyncMock()
        client.close = AsyncMock()
        client.getprop = (
            AsyncMock(side_effect=RuntimeError("unreadable")) if unreadable
            else AsyncMock(return_value="")
        )
        with pytest.raises(NoApprovedRecipe):
            await async_install_entry(hass, entry)
    install.assert_not_awaited()
    assert entry.data[CONF_DEVICE_PROFILE] is None
    client.close.assert_awaited_once()


async def test_KSM_TEST_271_exact_but_unassigned_model_remains_unprovisioned(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="Unassigned TV", data={
        "host": "192.168.99.99", "port": 5555, "key_path": "/tmp/test-key",
        CONF_DEVICE_PROFILE: None,
    })
    entry.add_to_hass(hass)
    unresolved = SimpleNamespace(model_key="unassigned_model", executable=False,
                                 reason="no approved assignment")
    with patch("custom_components.kiosk_satellite_manager.button.AdbClient") as cls, patch(
        "custom_components.kiosk_satellite_manager.button.resolve_catalog_entry",
        return_value=unresolved,
    ), patch("custom_components.kiosk_satellite_manager.button.install_and_launch",
             new=AsyncMock()) as install, patch(
        "custom_components.kiosk_satellite_manager.button.async_get_clientsession"
    ):
        client = cls.return_value
        client.connect = AsyncMock()
        client.close = AsyncMock()
        client.getprop = AsyncMock(return_value="")
        with pytest.raises(NoApprovedRecipe, match="no approved assignment"):
            await async_install_entry(hass, entry)
    install.assert_not_awaited()
    assert entry.data[CONF_DEVICE_PROFILE] is None


async def test_KSM_TEST_271_install_failure_does_not_persist_recovered_model(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="Theater GTV", data={
        "host": "192.168.99.99", "port": 5555, "key_path": "/tmp/test-key",
        CONF_DEVICE_PROFILE: None,
    })
    entry.add_to_hass(hass)
    props = {"ro.product.manufacturer": "onn", "ro.product.model": "onn 4K Pro Streaming Device",
             "ro.build.version.sdk": "34"}
    with patch("custom_components.kiosk_satellite_manager.button.AdbClient") as cls, patch(
        "custom_components.kiosk_satellite_manager.button.install_and_launch",
        new=AsyncMock(side_effect=RuntimeError("install failed")),
    ) as install, patch("custom_components.kiosk_satellite_manager.button.async_get_clientsession"):
        client = cls.return_value
        client.connect = AsyncMock()
        client.close = AsyncMock()
        client.getprop = AsyncMock(side_effect=lambda name: props.get(name, ""))
        with pytest.raises(RuntimeError, match="install failed"):
            await async_install_entry(hass, entry)
    assert install.await_args.kwargs["device_model"] == "onn_4k_pro_android14"
    assert entry.data[CONF_DEVICE_PROFILE] is None
    client.close.assert_awaited_once()


async def test_KSM_TEST_271_stored_profile_is_not_replaced_on_install(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="Portal", data={
        "host": "192.168.99.99", "port": 5555, "key_path": "/tmp/test-key",
        CONF_DEVICE_PROFILE: "portal_go",
    })
    entry.add_to_hass(hass)
    with patch("custom_components.kiosk_satellite_manager.button.AdbClient") as cls, patch(
        "custom_components.kiosk_satellite_manager.button.install_and_launch",
        new=AsyncMock(return_value=None),
    ) as install, patch("custom_components.kiosk_satellite_manager.button.async_get_clientsession"):
        client = cls.return_value
        client.connect = AsyncMock()
        client.close = AsyncMock()
        client.getprop = AsyncMock(side_effect=AssertionError("stored model must not be reprofiled"))
        await async_install_entry(hass, entry)
    assert install.await_args.kwargs["device_model"] == "portal_go"
    assert entry.data[CONF_DEVICE_PROFILE] == "portal_go"
    client.getprop.assert_not_awaited()


def _refuses_adb():
    """An AdbClient patch that fails loudly on construction -- proves the
    self-update path never even tries to build one."""
    return patch(
        "custom_components.kiosk_satellite_manager.button.AdbClient",
        side_effect=AssertionError("AdbClient must not be constructed"),
    )


def _release(version: str) -> ReleaseInfo:
    return ReleaseInfo(version, f"https://example.invalid/releases/{version}", f"notes for {version}")


def _entity_id(hass, entry_id: str, domain: str, suffix: str) -> str:
    ent_reg = er.async_get(hass)
    return next(
        e.entity_id
        for e in er.async_entries_for_config_entry(ent_reg, entry_id)
        if e.domain == domain and e.unique_id == f"{entry_id}_{suffix}"
    )


def _fake_apk_response():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.read = AsyncMock(return_value=b"fake-apk-bytes")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_health_response(app_version):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value={"appVersion": app_version})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def test_press_installs_launches_grants_and_refreshes_version(hass, tmp_path, release_check):
    # KSM-BEHAVE-147: the press installs the shared release check's latest.
    release_check.return_value = ReleaseInfo(
        "new", None, None, (("kiosk-satellite-new.apk", "https://example.invalid/ks.apk"),)
    )
    # fetch_health is called once by the coordinator's first refresh during
    # setup, and again by the bounded post-install poll -- both must stay
    # mocked for the whole test, or phacc's pytest-socket blocks the real
    # network call.
    health_responses = iter([{"appVersion": "old"}, {"appVersion": "new"}])

    async def fake_fetch_health(session, host, *, pin=None):
        return next(health_responses)

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        # The entry stores the exact device model the config flow matched
        # (issue #20); the button passes it straight to install_and_launch,
        # which resolves the approved recipe from it.
        # Portal Mini retains the launcher-capable v2 recipe. Portal Go's
        # observed OEM resolver is deliberately assigned launcher-free v3.
        # CONF_PASSWORD: None keeps this test on the ADB install path only --
        # install_and_launch's device-name/HA auto-connect sync (a stored
        # password) is covered separately and would need this fake_session to
        # also answer /api/setup/status, /api/login and PATCH /api/settings.
        ctx = await init_integration(
            hass, data={CONF_DEVICE_PROFILE: "portal_mini", CONF_PASSWORD: None, "replace_launcher": False}
        )
        registered = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, ctx.entry.entry_id), ctx.entry.entry_id,
        )
        assert registered is not None and registered.area_id == "ksm_test_area"

        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
        button_entry = next(e for e in entries if e.domain == "button")
        assert hass.data[DOMAIN][ctx.entry.entry_id].data["appVersion"] == "old"

        # KSM-BEHAVE-040: install_and_launch's own post-install health-poll
        # readback hits this same session (through the button's patched
        # async_get_clientsession), separately from the coordinator-level
        # fetch_health patched above -- route by URL so both the APK
        # download and /api/health get the response shape they expect.
        fake_session = MagicMock()

        def _session_get(url, **kwargs):
            if "/api/health" in url:
                return _fake_health_response("new")
            return _fake_apk_response()

        fake_session.get = MagicMock(side_effect=_session_get)

        coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
        seen_installing_during_press = False

        real_update_listeners = coordinator.async_update_listeners

        def _spy_update_listeners():
            nonlocal seen_installing_during_press
            if coordinator.ksm_installing:
                seen_installing_during_press = True
            return real_update_listeners()

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.install.latest_release",
            new=AsyncMock(side_effect=AssertionError("live release lookup")),
        ), patch(
            "custom_components.kiosk_satellite_manager.apk_cache.verify_ks_apk_signer"
        ), patch(
            "custom_components.kiosk_satellite_manager.apk_cache.cache_root",
            return_value=tmp_path / "apks",
        ), patch(
            "custom_components.kiosk_satellite_manager.button.async_get_clientsession",
            return_value=fake_session,
        ), patch(
            "custom_components.kiosk_satellite_manager.apk_cache.async_get_clientsession",
            return_value=fake_session,
        ), patch.object(
            coordinator, "async_update_listeners", side_effect=_spy_update_listeners
        ):
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock()
            mock_client.getprop = AsyncMock(return_value="armeabi-v7a")
            mock_client.push = AsyncMock()
            mock_client.install_apk = AsyncMock()
            mock_client.installed_version = AsyncMock(side_effect=[None, "new"])
            mock_client.shell = AsyncMock(return_value="")
            mock_client.close = AsyncMock()
            mock_client.granted_permissions = AsyncMock(return_value=set())
            mock_client.appop_mode = AsyncMock(return_value="allow")
            mock_client.is_battery_exempt = AsyncMock(return_value=True)
            mock_client.declared_bound_services = AsyncMock(return_value={})
            mock_client.get_secure_setting = AsyncMock(return_value="")
            mock_client.put_secure_setting = AsyncMock()
            mock_client.bluetooth_enabled = AsyncMock(return_value=True)
            # KSM-BEHAVE-152: unset Private DNS, turned off and read back.
            mock_client.get_global_setting = AsyncMock(side_effect=["", "off"])
            mock_client.put_global_setting = AsyncMock()
            mock_client.resolve_host = AsyncMock(return_value=None)

            await hass.services.async_call(
                "button", "press", {"entity_id": button_entry.entity_id}, blocking=True
            )

    mock_client.put_global_setting.assert_awaited_once_with("private_dns_mode", "off")
    assert ctx.entry.data[CONF_PRIVATE_DNS_PRIOR] == ""
    assert mock_client.push.await_count == 1
    mock_client.install_apk.assert_awaited_once()
    shell_calls = [c.args[0] for c in mock_client.shell.await_args_list]
    assert shell_calls[1] == "am start -n me.jxl.kiosk_satellite/.MainActivity"
    assert "dumpsys deviceidle whitelist +me.jxl.kiosk_satellite" in shell_calls
    assert "appops set me.jxl.kiosk_satellite SYSTEM_ALERT_WINDOW allow" in shell_calls
    assert all("ks.provision" not in call for call in shell_calls)
    assert all("set-home-activity" not in call for call in shell_calls)
    assert seen_installing_during_press is True
    assert coordinator.ksm_installing is False
    assert hass.data[DOMAIN][ctx.entry.entry_id].data["appVersion"] == "new"


@pytest.mark.parametrize(
    ("stored_token", "returned_token", "expected_token"),
    [
        (None, TokenCredential("new-device-token", "new-refresh", True), "new-device-token"),
        ("existing-device-token", TokenCredential("replacement-token", "replacement-refresh", True), "existing-device-token"),
    ],
)
async def test_press_persists_only_a_new_device_token(
    hass, stored_token, returned_token, expected_token
):
    """[KSM-TEST-087] A first install saves its device token once, while a
    reinstall keeps the configured credential rather than replacing it."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    entry_data = {CONF_DEVICE_PROFILE: "portal_go"}
    if stored_token is not None:
        entry_data[CONF_HA_TOKEN] = stored_token

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass, data=entry_data)
        ent_reg = er.async_get(hass)
        install_entry = next(
            entry
            for entry in er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
            if entry.unique_id == f"{ctx.entry.entry_id}_install"
        )

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.button.install_and_launch",
            new=AsyncMock(return_value=returned_token),
        ):
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock()
            mock_client.close = AsyncMock()

            await hass.services.async_call(
                "button", "press", {"entity_id": install_entry.entity_id}, blocking=True
            )

    assert ctx.entry.data.get(CONF_HA_TOKEN) == expected_token
    mock_client.close.assert_awaited_once()


async def test_auto_credential_recovery_rotates_after_the_replacement_is_verified(hass):
    """[KSM-TEST-110] Auto credentials rotate; selected tokens never do."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    entry_data = {
        CONF_DEVICE_PROFILE: "portal_go",
        CONF_HA_TOKEN: "old-kiosk-token",
        CONF_HA_REFRESH_TOKEN_ID: "old-refresh",
        CONF_HA_TOKEN_OWNED: True,
        CONF_TOKEN_MODE: TOKEN_MODE_AUTO,
    }
    new_credential = TokenCredential("new-kiosk-token", "new-refresh", True)
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass, data=entry_data)
        ent_reg = er.async_get(hass)
        install_entry = next(
            entry for entry in er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
            if entry.unique_id == f"{ctx.entry.entry_id}_install"
        )
        with patch("custom_components.kiosk_satellite_manager.button.AdbClient") as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.button.install_and_launch",
            new=AsyncMock(return_value=new_credential),
        ) as install, patch(
            "custom_components.kiosk_satellite_manager.button.async_replace_entry_credential",
            new=AsyncMock(),
        ) as replace:
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock()
            mock_client.close = AsyncMock()
            await hass.services.async_call(
                "button", "press", {"entity_id": install_entry.entity_id}, blocking=True
            )

    assert install.await_args.kwargs["ha_token"] is None
    assert install.await_args.kwargs["token_credential"] is None
    replace.assert_awaited_once_with(hass, ctx.entry, new_credential)


async def test_press_retries_health_until_success_without_a_terminal_delay(hass):
    """[KSM-TEST-088] Post-install health polling stops at the first healthy
    response; a permanently unhealthy device gets exactly the bounded delays
    *between* attempts, never an extra delay after the final request."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_go"})
        ent_reg = er.async_get(hass)
        install_entry = next(
            entry
            for entry in er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
            if entry.unique_id == f"{ctx.entry.entry_id}_install"
        )
        coordinator = hass.data[DOMAIN][ctx.entry.entry_id]

        refresh_count = 0

        async def always_unhealthy():
            nonlocal refresh_count
            refresh_count += 1
            coordinator.last_update_success = False

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.button.install_and_launch",
            new=AsyncMock(return_value=None),
        ), patch.object(
            coordinator, "async_request_refresh", new=AsyncMock(side_effect=always_unhealthy)
        ) as mock_refresh, patch(
            "custom_components.kiosk_satellite_manager.button.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep:
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock()
            mock_client.close = AsyncMock()

            await hass.services.async_call(
                "button", "press", {"entity_id": install_entry.entity_id}, blocking=True
            )

    assert refresh_count == INSTALL_LAUNCH_POLL_ATTEMPTS
    assert mock_refresh.await_count == INSTALL_LAUNCH_POLL_ATTEMPTS
    assert mock_sleep.await_count == INSTALL_LAUNCH_POLL_ATTEMPTS - 1
    assert coordinator.ksm_installing is False
    mock_client.close.assert_awaited_once()


async def test_press_stops_health_retries_at_the_first_success(hass):
    """[KSM-TEST-089] A successful refresh stops the bounded retry loop
    immediately instead of continuing to poll a now-healthy device."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_go"})
        ent_reg = er.async_get(hass)
        install_entry = next(
            entry
            for entry in er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
            if entry.unique_id == f"{ctx.entry.entry_id}_install"
        )
        coordinator = hass.data[DOMAIN][ctx.entry.entry_id]

        refresh_count = 0

        async def healthy_on_second_refresh():
            nonlocal refresh_count
            refresh_count += 1
            coordinator.last_update_success = refresh_count == 2

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.button.install_and_launch",
            new=AsyncMock(return_value=None),
        ), patch.object(
            coordinator,
            "async_request_refresh",
            new=AsyncMock(side_effect=healthy_on_second_refresh),
        ) as mock_refresh, patch(
            "custom_components.kiosk_satellite_manager.button.asyncio.sleep", new=AsyncMock()
        ) as mock_sleep:
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock()
            mock_client.close = AsyncMock()

            await hass.services.async_call(
                "button", "press", {"entity_id": install_entry.entity_id}, blocking=True
            )

    assert mock_refresh.await_count == 2
    mock_sleep.assert_awaited_once()
    mock_client.close.assert_awaited_once()


async def test_press_cleans_up_installing_state_and_connection_after_install_failure(hass):
    """[KSM-TEST-090] Once connected, a failed install still closes ADB and
    clears the transient sensor state so the button never leaves it stuck on
    Installing."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_go"})
        ent_reg = er.async_get(hass)
        install_entry = next(
            entry
            for entry in er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
            if entry.unique_id == f"{ctx.entry.entry_id}_install"
        )
        coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
        real_update_listeners = coordinator.async_update_listeners
        mock_update_listeners = MagicMock(wraps=real_update_listeners)

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.button.install_and_launch",
            new=AsyncMock(side_effect=RuntimeError("install failed")),
        ), patch.object(
            coordinator, "async_update_listeners", new=mock_update_listeners
        ):
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock()
            mock_client.close = AsyncMock()

            with pytest.raises(RuntimeError, match="install failed"):
                await hass.services.async_call(
                    "button", "press", {"entity_id": install_entry.entity_id}, blocking=True
                )

    assert coordinator.ksm_installing is False
    assert mock_update_listeners.call_count == 2
    mock_client.close.assert_awaited_once()


async def test_press_cleans_up_installing_state_after_connection_failure(hass):
    """[KSM-TEST-091] A rejected ADB connection never leaves the version
    sensor in Installing, and does not try to close an unacquired session."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass)
        ent_reg = er.async_get(hass)
        install_entry = next(
            entry
            for entry in er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
            if entry.unique_id == f"{ctx.entry.entry_id}_install"
        )
        coordinator = hass.data[DOMAIN][ctx.entry.entry_id]

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock(side_effect=RuntimeError("connect failed"))
            mock_client.close = AsyncMock()

            with pytest.raises(RuntimeError, match="connect failed"):
                await hass.services.async_call(
                    "button", "press", {"entity_id": install_entry.entity_id}, blocking=True
                )

    assert coordinator.ksm_installing is False
    mock_client.close.assert_not_awaited()


async def test_press_succeeds_when_the_entry_coordinator_is_missing(hass):
    """[KSM-TEST-092] A button left behind during coordinator teardown still
    installs safely; optional refresh bookkeeping cannot block recovery."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_go"})
        ent_reg = er.async_get(hass)
        install_entry = next(
            entry
            for entry in er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
            if entry.unique_id == f"{ctx.entry.entry_id}_install"
        )
        hass.data[DOMAIN].pop(ctx.entry.entry_id)

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.button.install_and_launch",
            new=AsyncMock(return_value=None),
        ):
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock()
            mock_client.close = AsyncMock()

            await hass.services.async_call(
                "button", "press", {"entity_id": install_entry.entity_id}, blocking=True
            )

    mock_client.close.assert_awaited_once()


async def test_uninstall_button_press_uninstalls_ks(hass):
    """[KSM-TEST-008] The cleanup button connects over ADB, uninstalls Kiosk
    Satellite, and disconnects."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "2026.9.62"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass)

        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
        assert len([e for e in entries if e.domain == "button"]) == 4  # install, uninstall, backup, restore
        uninstall_entry = next(
            e for e in entries if e.unique_id == f"{ctx.entry.entry_id}_uninstall"
        )
        assert hass.states.get(uninstall_entry.entity_id).name.endswith(
            "Uninstall Kiosk Satellite"
        )
        # [KSM-TEST-288] a destructive action is a Configuration control,
        # kept off the device's default control surface.
        assert uninstall_entry.entity_category == EntityCategory.CONFIG

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock()
            mock_client.uninstall_ks = AsyncMock()
            mock_client.close = AsyncMock()

            await hass.services.async_call(
                "button",
                "press",
                {"entity_id": uninstall_entry.entity_id},
                blocking=True,
            )

    mock_client.connect.assert_awaited_once()
    mock_client.uninstall_ks.assert_awaited_once()
    mock_client.close.assert_awaited_once()


async def test_press_refuses_to_provision_a_device_with_no_approved_recipe(hass):
    """[KSM-TEST-060] An entry created for a device the catalog could not
    identify carries no model key. Pressing Install must fail closed rather
    than fall back to the Meta Portal recipe, and must not touch the device:
    no APK push, no pm install, no permission grants."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass)

        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
        install_entry = next(
            e for e in entries if e.unique_id == f"{ctx.entry.entry_id}_install"
        )
        assert ctx.entry.data.get(CONF_DEVICE_PROFILE) is None

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock()
            mock_client.getprop = AsyncMock(return_value="armeabi-v7a")
            mock_client.push = AsyncMock()
            mock_client.install_apk = AsyncMock()
            mock_client.shell = AsyncMock(return_value="")
            mock_client.close = AsyncMock()

            with pytest.raises(NoApprovedRecipe):
                await hass.services.async_call(
                    "button",
                    "press",
                    {"entity_id": install_entry.entity_id},
                    blocking=True,
                )

    mock_client.push.assert_not_awaited()
    mock_client.install_apk.assert_not_awaited()
    mock_client.shell.assert_not_awaited()
    # The ADB session is still closed cleanly -- failing closed is not
    # failing messily.
    mock_client.close.assert_awaited_once()


async def test_install_press_names_adb_and_host_port_on_connect_refusal(hass):
    """[KSM-TEST-158] negative case: the Install/Reinstall button with ADB
    refused raises an error naming ADB and host:port, not a bare
    AdbConnectFailed (KSM-BEHAVE-081)."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass)
        install_entity_id = _entity_id(hass, ctx.entry.entry_id, "button", "install")

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls:
            mock_client_cls.return_value.connect = AsyncMock(
                side_effect=AdbConnectFailed("connection refused")
            )
            with pytest.raises(HomeAssistantError, match=r"ADB is unreachable at .+:\d+"):
                await hass.services.async_call(
                    "button", "press", {"entity_id": install_entity_id}, blocking=True
                )


async def test_uninstall_press_names_adb_and_host_port_on_connect_refusal(hass):
    """[KSM-TEST-158] negative case: the Uninstall button with ADB refused
    raises an error naming ADB and host:port, not a bare AdbConnectFailed."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass)
        uninstall_entity_id = _entity_id(hass, ctx.entry.entry_id, "button", "uninstall")

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls:
            mock_client_cls.return_value.connect = AsyncMock(
                side_effect=AdbConnectFailed("connection refused")
            )
            with pytest.raises(HomeAssistantError, match=r"ADB is unreachable at .+:\d+"):
                await hass.services.async_call(
                    "button", "press", {"entity_id": uninstall_entity_id}, blocking=True
                )


async def test_update_all_updates_two_devices_over_the_ks_api(hass, release_check):
    """[KSM-TEST-158] two outdated devices -- one that confirms the new
    version immediately, one that still needs the on-device confirmation tap
    -- both update through the KS API, regardless of ADB stub state. The
    notification reports all four buckets; no AdbClient is constructed."""
    release_check.return_value = _release("2026.9.77")

    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "2026.9.76"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        first = await init_integration(hass)
        second = await init_integration(hass, data={CONF_HOST: "192.168.99.98"})
        manager = next(
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER
        )
        update_all_entity_id = _entity_id(hass, manager.entry_id, "button", "update_all")

        async def fake_poll_health(session, host, *, pin=None):
            # The first device's poll sees the new version land immediately;
            # the second's never does, so its outcome resolves from
            # getUpdateStatus's lastOutcome instead (KSM-TEST-155/158).
            if host == "192.168.99.98":
                return {"appVersion": "2026.9.76"}
            return {"appVersion": "2026.9.77"}

        def fake_run_command(session, host, token, command, *, pin=None):
            responses = {
                "getUpdateStatus": {
                    "ok": True,
                    "data": {"lastOutcome": "confirm"} if host == "192.168.99.98" else {},
                },
                "installUploadedApk": {"ok": True},
            }
            return responses[command]

        with _refuses_adb() as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.ks_update.SELF_UPDATE_POLL_ATTEMPTS", 1
        ), patch(_POLL_HEALTH, new=fake_poll_health), patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(
            _RUN_COMMAND, new=AsyncMock(side_effect=fake_run_command)
        ), patch(
            "custom_components.kiosk_satellite_manager.button.persistent_notification.async_create"
        ) as notify:
            await hass.services.async_call(
                "button", "press", {"entity_id": update_all_entity_id}, blocking=True
            )

    mock_client_cls.assert_not_called()
    message = notify.call_args.kwargs["message"]
    assert first.entry.title in message
    assert second.entry.title in message
    assert f"Updated: {first.entry.title}" in message
    assert f"Awaiting confirmation on device: {second.entry.title}" in message
    assert "Skipped:" in message
    assert "Failed: none" in message


@pytest.mark.parametrize(
    ("profile", "stored", "expected"),
    [
        ("portal_mini", None, True),
        ("portal_mini", False, False),
        ("onn_4k_pro_android14", None, False),
        ("onn_4k_pro_android14", True, True),
    ],
)
async def test_KSM_TEST_282_install_press_passes_launcher_choice(hass, profile, stored, expected):
    """[KSM-TEST-282] Install passes the stored choice, or the recipe default."""
    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    data = {CONF_DEVICE_PROFILE: profile, CONF_PASSWORD: None}
    if stored is not None:
        data["replace_launcher"] = stored
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass, data=data)
        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as client_cls, patch(
            "custom_components.kiosk_satellite_manager.button.install_and_launch",
            new=AsyncMock(return_value=None),
        ) as install:
            client_cls.return_value.connect = AsyncMock()
            client_cls.return_value.close = AsyncMock()
            await async_install_entry(hass, ctx.entry)
    assert install.await_args.kwargs["replace_launcher"] is expected


@pytest.mark.parametrize("pinned", [False, True])
async def test_install_re_establishes_https_only_on_an_opted_in_entry(hass, pinned):
    """[KSM-TEST-335] #137: Install switches transport only for an entry the
    operator already opted into HTTPS; the pin it stores clears both TLS
    repairs."""
    pin = "ab" * 32

    async def fake_fetch_health(session, host, *, pin=None):
        return {"appVersion": "old"}

    data = {CONF_DEVICE_PROFILE: "portal_go"}
    if pinned:
        data[CONF_TLS_SPKI] = pin
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass, data=data)
        install_entry = next(
            e for e in er.async_entries_for_config_entry(er.async_get(hass), ctx.entry.entry_id)
            if e.unique_id == f"{ctx.entry.entry_id}_install"
        )
        install = AsyncMock(return_value=None)
        with patch("custom_components.kiosk_satellite_manager.button.AdbClient") as mock_client_cls, \
                patch("custom_components.kiosk_satellite_manager.button.install_and_launch", new=install):
            mock_client_cls.return_value.connect = AsyncMock()
            mock_client_cls.return_value.close = AsyncMock()
            await hass.services.async_call(
                "button", "press", {"entity_id": install_entry.entity_id}, blocking=True
            )

    kwargs = install.await_args.kwargs
    assert kwargs["establish_tls"] is pinned
    issue_ids = [f"tls_disabled_{ctx.entry.entry_id}", f"tls_certificate_changed_{ctx.entry.entry_id}"]
    for issue_id in issue_ids:
        ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=True,
                              severity=ir.IssueSeverity.ERROR, translation_key="tls_disabled")
    kwargs["on_tls_pinned"]("cd" * 32)
    assert ctx.entry.data[CONF_TLS_SPKI] == "cd" * 32
    assert all(ir.async_get(hass).async_get_issue(DOMAIN, i) is None for i in issue_ids)
