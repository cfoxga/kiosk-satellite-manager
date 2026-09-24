"""Manager entry, release status, and fleet update contracts (issue #44)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import voluptuous as vol

from homeassistant import config_entries, data_entry_flow
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager.const import (
    CONF_AUTO_UPDATE, CONF_AUTO_UPDATE_ALL, CONF_ENTRY_TYPE, CONF_HA_URL, CONF_HOST,
    CONF_ONBOARDING_MODE, CONF_PASSWORD, CONF_TOKEN_MODE, DOMAIN,
    MANAGER_UPDATE_RUNNING_KEY, ONBOARDING_AUTOMATIC, RELEASE_COORDINATOR_KEY,
    TOKEN_MODE_AUTO,
)
from custom_components.kiosk_satellite_manager.ks_api import ReleaseInfo

from .conftest import init_integration
from .test_config_flow import _getprop, _PORTAL_GO_PROPS
from .test_config_flow import _GTV_PROPS


async def _manager(hass, *, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Kiosk Satellite Manager",
        unique_id="ksm_manager", data={CONF_ENTRY_TYPE: "manager"}, options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_manager_entry_has_no_adb_and_coexists_with_device(hass, release_check):
    """[KSM-TEST-137/138/143] Manager loads without ADB and shares release state."""
    with patch("custom_components.kiosk_satellite_manager.AdbClient") as adb, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.0"}),
    ):
        manager = await _manager(hass)
        assert not adb.called
        release = hass.data[RELEASE_COORDINATOR_KEY]
        device = await init_integration(hass)
        assert hass.data[RELEASE_COORDINATOR_KEY] is release
        assert release_check.await_count == 1
        manager_entities = er.async_entries_for_config_entry(er.async_get(hass), manager.entry_id)
        assert {e.domain for e in manager_entities} == {"sensor", "button", "switch"}
        assert await hass.config_entries.async_unload(manager.entry_id)
        assert hass.data[RELEASE_COORDINATOR_KEY] is release
        assert device.entry.entry_id in hass.data[DOMAIN]


async def test_manager_entry_auto_created_when_missing(hass, release_check):
    """[KSM-TEST-147] KSM-BEHAVE-078: a device entry setup with no manager
    entry present creates one automatically, and a second device setup does
    not create a second one."""
    with patch("custom_components.kiosk_satellite_manager.AdbClient"), patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.0"}),
    ):
        assert not any(
            e.data.get(CONF_ENTRY_TYPE) == "manager"
            for e in hass.config_entries.async_entries(DOMAIN)
        )
        await init_integration(hass)

        managers = [
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == "manager"
        ]
        assert len(managers) == 1
        manager_entities = er.async_entries_for_config_entry(
            er.async_get(hass), managers[0].entry_id
        )
        assert {e.domain for e in manager_entities} == {"sensor", "button", "switch"}

        await init_integration(hass, data={CONF_HOST: "192.168.99.100"})
        managers = [
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get(CONF_ENTRY_TYPE) == "manager"
        ]
        assert len(managers) == 1


async def test_manager_flow_is_unique_and_device_flow_stays_available(hass):
    """[KSM-TEST-137] Explicit manager choice is unique."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "manager"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    entry = MockConfigEntry(domain=DOMAIN, data=result["data"], unique_id="ksm_manager")
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "manager"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT


async def test_device_flow_requires_address(hass):
    """[KSM-TEST-137] Choosing a device never creates a hostless entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTRY_TYPE: "device"}
    )
    assert result["errors"][CONF_HOST] == "host_required"


async def test_update_all_skips_current_and_continues_after_failure(hass, release_check):
    """[KSM-TEST-145/146] Fleet action uses entry install and reports outcomes."""
    release_check.return_value = ReleaseInfo("2026.9.2", "https://example.invalid/2", "notes")
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(side_effect=lambda session, host: {
            "appVersion": "2026.9.2" if host.endswith(".3") else "2026.9.1"
        }),
    ):
        manager = await _manager(hass)
        await init_integration(hass, data={"host": "192.168.99.1"})
        await init_integration(hass, data={"host": "192.168.99.2"})
        await init_integration(hass, data={"host": "192.168.99.3"})
        entity = next(
            e for e in er.async_entries_for_config_entry(er.async_get(hass), manager.entry_id)
            if e.domain == "button"
        )
        with patch(
            "custom_components.kiosk_satellite_manager.button.async_self_update_entry",
            new=AsyncMock(side_effect=[RuntimeError("failed"), None]),
        ) as install, patch(
            "custom_components.kiosk_satellite_manager.button.persistent_notification.async_create"
        ) as notify:
            await hass.services.async_call(
                "button", "press", {"entity_id": entity.entity_id}, blocking=True
            )
        assert install.await_count == 2
        message = notify.call_args.kwargs["message"]
        assert "Failed:" in message and "Updated:" in message and "Skipped:" in message
        assert "current, skipped, or unavailable" in message


async def test_update_all_refuses_overlap_and_unknown_release(hass, release_check):
    """[KSM-TEST-146] No release and another run start no install."""
    manager = await _manager(hass)
    entity = next(
        e for e in er.async_entries_for_config_entry(er.async_get(hass), manager.entry_id)
        if e.domain == "button"
    )
    with patch("custom_components.kiosk_satellite_manager.button.async_self_update_entry") as install, patch(
        "custom_components.kiosk_satellite_manager.button.persistent_notification.async_create"
    ) as notify:
        hass.data[MANAGER_UPDATE_RUNNING_KEY] = True
        await hass.services.async_call("button", "press", {"entity_id": entity.entity_id}, blocking=True)
        assert "already running" in notify.call_args.kwargs["message"]
        hass.data.pop(MANAGER_UPDATE_RUNNING_KEY)
        hass.data[RELEASE_COORDINATOR_KEY].async_set_updated_data(None)
        await hass.services.async_call("button", "press", {"entity_id": entity.entity_id}, blocking=True)
        assert "No usable" in notify.call_args.kwargs["message"]
        install.assert_not_called()


async def test_update_all_skips_unreachable_installing_and_ha_skipped(hass, release_check):
    """[KSM-TEST-145] Fleet action respects health, install lock, and HA skip."""
    release_check.return_value = ReleaseInfo("2026.9.2", "https://example.invalid/2", "notes")
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.1"}),
    ):
        manager = await _manager(hass)
        busy = await init_integration(hass, data={CONF_HOST: "192.168.99.1"})
        unreachable = await init_integration(hass, data={CONF_HOST: "192.168.99.2"})
        skipped = await init_integration(hass, data={CONF_HOST: "192.168.99.3"})
        hass.data[DOMAIN][busy.entry.entry_id].ksm_installing = True
        hass.data[DOMAIN][unreachable.entry.entry_id].async_set_update_error(
            UpdateFailed("unreachable")
        )
        update = next(
            e for e in er.async_entries_for_config_entry(er.async_get(hass), skipped.entry.entry_id)
            if e.domain == "update"
        )
        await hass.services.async_call(
            "update", "skip", {"entity_id": update.entity_id}, blocking=True
        )
        button = next(
            e for e in er.async_entries_for_config_entry(er.async_get(hass), manager.entry_id)
            if e.domain == "button"
        )
        with patch(
            "custom_components.kiosk_satellite_manager.button.async_self_update_entry"
        ) as install, patch(
            "custom_components.kiosk_satellite_manager.button.persistent_notification.async_create"
        ) as notify:
            await hass.services.async_call(
                "button", "press", {"entity_id": button.entity_id}, blocking=True
            )
        install.assert_not_called()
        assert "unreachable" in notify.call_args.kwargs["message"]
        assert "install in progress" in notify.call_args.kwargs["message"]


async def test_manager_options_mask_password_preserve_blank_and_reject_bad_url(hass):
    """[KSM-TEST-142] Secret edits preserve blank and URL must be absolute HTTP(S)."""
    manager = await _manager(hass, options={CONF_PASSWORD: "saved-secret"})
    result = await hass.config_entries.options.async_init(manager.entry_id)
    assert result["step_id"] == "init"
    password_field = next(k for k in result["data_schema"].schema if k == CONF_PASSWORD)
    assert result["data_schema"].schema[password_field].config["type"] == "password"
    assert password_field.default is vol.UNDEFINED
    data = result["data_schema"]({})
    data.update({
        CONF_PASSWORD: "", CONF_HA_URL: "javascript:alert(1)",
        CONF_TOKEN_MODE: TOKEN_MODE_AUTO,
    })
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["errors"][CONF_HA_URL] == "invalid_ha_url"
    data[CONF_HA_URL] = "https://ha.example.test"
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert manager.options[CONF_PASSWORD] == "saved-secret"
    assert manager.options[CONF_HA_URL] == "https://ha.example.test"
    assert "ha_token" not in manager.options


async def test_options_reject_revoked_token_and_device_entry(hass):
    """[KSM-TEST-138/142] Device entries cannot store global options; stale token fails."""
    manager = await _manager(hass)
    result = await hass.config_entries.options.async_init(manager.entry_id)
    data = result["data_schema"]({})
    data.update({CONF_HA_URL: "https://ha.example.test", CONF_TOKEN_MODE: "revoked"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["errors"][CONF_TOKEN_MODE] == "token_not_found"
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.1"}),
    ):
        device = await init_integration(hass)
    result = await hass.config_entries.options.async_init(device.entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.ABORT


async def test_options_accept_selected_token_id_and_new_password(hass):
    """[KSM-TEST-142] Manager stores an identifier, never the token value."""
    manager = await _manager(hass)
    token = SimpleNamespace(id="token-id", token_type="long_lived_access_token")
    with patch.object(hass.auth, "async_get_refresh_token", return_value=token):
        result = await hass.config_entries.options.async_init(manager.entry_id)
        data = result["data_schema"]({})
        data.update({
            CONF_HA_URL: "https://ha.example.test", CONF_TOKEN_MODE: "token-id",
            CONF_PASSWORD: "new-secret",
        })
        result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert manager.options[CONF_TOKEN_MODE] == "token-id"
    assert manager.options[CONF_PASSWORD] == "new-secret"
    assert "ha_token" not in manager.options


async def test_manager_release_sensor_retains_last_good_on_failed_check(hass, release_check):
    """[KSM-TEST-144] Failure marks status stale but keeps last usable version."""
    manager = await _manager(hass)
    entity = next(
        e for e in er.async_entries_for_config_entry(er.async_get(hass), manager.entry_id)
        if e.domain == "sensor"
    )
    before = hass.states.get(entity.entity_id)
    assert before.state == "2026.9.1"
    assert before.attributes["last_successful_check"]
    release_check.side_effect = RuntimeError("offline")
    await hass.data[RELEASE_COORDINATOR_KEY].async_request_refresh()
    after = hass.states.get(entity.entity_id)
    assert after.state == "2026.9.1"
    assert after.attributes["check_status"] == "stale"


async def _device_flow(hass, *, installed=False, props=None):
    with patch("custom_components.kiosk_satellite_manager.config_flow.AdbClient") as cls:
        client = cls.return_value
        client.connect = AsyncMock()
        client.getprop = _getprop(**(props or _PORTAL_GO_PROPS))
        client.shell = AsyncMock(return_value="Portal Go")
        client.is_ks_installed = AsyncMock(return_value=installed)
        client.close = AsyncMock()
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.99.5", "port": 5555}
        )


async def test_review_flow_snapshots_defaults_and_allows_override(hass):
    """[KSM-TEST-139/141] Defaults are copied once and operator can override."""
    manager = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: "manager"}, options={
        CONF_PASSWORD: "global-secret", CONF_HA_URL: "https://ha.example.test",
        CONF_AUTO_UPDATE: True, CONF_TOKEN_MODE: TOKEN_MODE_AUTO,
    })
    manager.add_to_hass(hass)
    result = await _device_flow(hass)
    assert result["step_id"] == "device_info"
    defaults = {
        k.schema: k.default() if callable(k.default) else k.default
        for k in result["data_schema"].schema
    }
    assert defaults[CONF_PASSWORD] == "global-secret"
    assert defaults[CONF_HA_URL] == "https://ha.example.test"
    assert defaults[CONF_AUTO_UPDATE] is True
    hass.config_entries.async_update_entry(manager, options={CONF_PASSWORD: "later-secret"})
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch",
        new=AsyncMock(return_value=None),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.1"}),
    ):
        cls.return_value.connect = AsyncMock()
        cls.return_value.close = AsyncMock()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            CONF_PASSWORD: "override", CONF_TOKEN_MODE: TOKEN_MODE_AUTO,
            CONF_HA_URL: "https://other.example.test", CONF_AUTO_UPDATE: False,
        })
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS_DONE:
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["data"][CONF_PASSWORD] == "override"
    assert result["data"][CONF_HA_URL] == "https://other.example.test"
    assert result["options"][CONF_AUTO_UPDATE] is False


async def test_review_rejects_invalid_ha_url_before_install(hass):
    """[KSM-TEST-139] A bad URL cannot be copied to a device entry."""
    manager = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: "manager"}, options={
        CONF_PASSWORD: "global-secret", CONF_HA_URL: "https://ha.example.test",
    })
    manager.add_to_hass(hass)
    result = await _device_flow(hass)
    with patch("custom_components.kiosk_satellite_manager.config_flow.install_and_launch") as install:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            CONF_PASSWORD: "global-secret", CONF_TOKEN_MODE: TOKEN_MODE_AUTO,
            CONF_HA_URL: "ftp://invalid.example",
        })
    assert result["errors"][CONF_HA_URL] == "invalid_ha_url"
    install.assert_not_called()


async def test_automatic_requires_confirmation_and_password_before_install(hass):
    """[KSM-TEST-140/141] Automatic mode cannot mutate on missing defaults."""
    manager = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: "manager"}, options={
        CONF_ONBOARDING_MODE: ONBOARDING_AUTOMATIC,
        "existing_install_action": "reinstall",
    })
    manager.add_to_hass(hass)
    result = await _device_flow(hass, installed=True)
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["action"] == "reinstall"
    with patch("custom_components.kiosk_satellite_manager.config_flow.install_and_launch") as install:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "confirm"
    assert result["errors"]["base"] == "password_required"
    install.assert_not_called()


async def test_automatic_reinstall_waits_for_confirmation_and_copies_settings(hass):
    """[KSM-TEST-140/142] Confirmation precedes uninstall and copies defaults."""
    manager = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: "manager"}, options={
        CONF_ONBOARDING_MODE: ONBOARDING_AUTOMATIC,
        "existing_install_action": "reinstall",
        CONF_PASSWORD: "global-secret", CONF_HA_URL: "https://ha.example.test",
        CONF_AUTO_UPDATE: True, CONF_TOKEN_MODE: TOKEN_MODE_AUTO,
    })
    manager.add_to_hass(hass)
    result = await _device_flow(hass, installed=True)
    assert result["step_id"] == "confirm"
    with patch("custom_components.kiosk_satellite_manager.config_flow.AdbClient") as cls, patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch",
        new=AsyncMock(return_value=None),
    ) as install, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.1"}),
    ):
        client = cls.return_value
        client.connect = AsyncMock()
        client.close = AsyncMock()
        client.uninstall_ks = AsyncMock()
        client.uninstall_ks.assert_not_awaited()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS_DONE:
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        client.uninstall_ks.assert_awaited_once()
        install.assert_awaited_once()
    assert result["data"][CONF_HA_URL] == "https://ha.example.test"
    assert result["data"][CONF_TOKEN_MODE] == TOKEN_MODE_AUTO
    assert result["options"][CONF_AUTO_UPDATE] is True


async def test_automatic_revoked_token_stops_before_install(hass):
    """[KSM-TEST-141] Selected token is checked when new flow confirms."""
    manager = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: "manager"}, options={
        CONF_ONBOARDING_MODE: ONBOARDING_AUTOMATIC,
        CONF_PASSWORD: "global-secret", CONF_TOKEN_MODE: "revoked-token",
    })
    manager.add_to_hass(hass)
    result = await _device_flow(hass)
    with patch("custom_components.kiosk_satellite_manager.config_flow.install_and_launch") as install:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "confirm"
    assert result["errors"]["base"] == "token_not_found"
    install.assert_not_called()


async def test_automatic_unknown_device_stops_before_install(hass):
    """[KSM-TEST-141] Unknown model never receives an install recipe."""
    manager = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: "manager"}, options={
        CONF_ONBOARDING_MODE: ONBOARDING_AUTOMATIC, CONF_PASSWORD: "global-secret",
    })
    manager.add_to_hass(hass)
    result = await _device_flow(hass, props=_GTV_PROPS)
    with patch("custom_components.kiosk_satellite_manager.config_flow.install_and_launch") as install:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "confirm"
    assert result["errors"]["base"] == "unsupported_device"
    install.assert_not_called()


async def test_automatic_invalid_global_url_stops_before_install(hass):
    """[KSM-TEST-141] A malformed saved override fails closed."""
    manager = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: "manager"}, options={
        CONF_ONBOARDING_MODE: ONBOARDING_AUTOMATIC, CONF_PASSWORD: "global-secret",
        CONF_HA_URL: "ftp://invalid.example",
    })
    manager.add_to_hass(hass)
    result = await _device_flow(hass)
    with patch("custom_components.kiosk_satellite_manager.config_flow.install_and_launch") as install:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"]["base"] == "invalid_ha_url"
    install.assert_not_called()


async def test_release_sensor_unavailable_before_first_success(hass, release_check):
    """[KSM-TEST-144] A failed first check cannot show an invented release."""
    release_check.side_effect = RuntimeError("offline")
    manager = await _manager(hass)
    entity = next(
        e for e in er.async_entries_for_config_entry(er.async_get(hass), manager.entry_id)
        if e.domain == "sensor"
    )
    assert hass.states.get(entity.entity_id).state == "unavailable"


_AUTO_INSTALL = "custom_components.kiosk_satellite_manager.update.async_self_update_entry"


def _health_version(version):
    return AsyncMock(return_value={"appVersion": version})


def _entity(hass, entry, suffix):
    return next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id == f"{entry.entry_id}_{suffix}"
    )


async def _publish(hass, release_check, version):
    release_check.return_value = ReleaseInfo(version, f"https://example.invalid/{version}", "notes")
    await hass.data[RELEASE_COORDINATOR_KEY].async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_auto_update_all_switch_defaults_off_and_touches_only_manager(hass):
    """[KSM-TEST-151] Fleet switch persists a manager option only, survives options edits."""
    manager = await _manager(hass, options={CONF_PASSWORD: "saved-secret"})
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=_health_version("2026.9.1")):
        device = await init_integration(hass)
    switch_id = _entity(hass, manager, "auto_update_all")
    assert hass.states.get(switch_id).state == "off"

    await hass.services.async_call("switch", "turn_on", {"entity_id": switch_id}, blocking=True)
    assert hass.states.get(switch_id).state == "on"
    assert manager.options[CONF_AUTO_UPDATE_ALL] is True
    assert CONF_AUTO_UPDATE not in device.entry.options
    assert hass.states.get(_entity(hass, device.entry, "auto_update")).state == "off"

    result = await hass.config_entries.options.async_init(manager.entry_id)
    data = result["data_schema"]({})
    data.update({CONF_HA_URL: "https://ha.example.test", CONF_TOKEN_MODE: TOKEN_MODE_AUTO})
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert manager.options[CONF_AUTO_UPDATE_ALL] is True
    assert hass.states.get(switch_id).state == "on"

    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=True)
    assert manager.options[CONF_AUTO_UPDATE_ALL] is False


async def test_auto_update_all_installs_new_release_on_opted_out_device(hass, release_check):
    """[KSM-TEST-152] Fleet on + device switch off -> one install per new release."""
    release_check.return_value = ReleaseInfo("2026.9.76", "https://example.invalid/76", "notes")
    await _manager(hass, options={CONF_AUTO_UPDATE_ALL: True})
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health", new=_health_version("2026.9.76")
    ), patch(_AUTO_INSTALL, new=AsyncMock()) as install:
        device = await init_integration(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        install.assert_not_awaited()
        await _publish(hass, release_check, "2026.9.77")
        await _publish(hass, release_check, "2026.9.77")
    install.assert_awaited_once()
    assert install.await_args.args[1] is device.entry


async def test_auto_update_all_off_leaves_opted_out_device_alone(hass, release_check):
    """[KSM-TEST-152] negative case: fleet off and device off -> no install."""
    release_check.return_value = ReleaseInfo("2026.9.76", "https://example.invalid/76", "notes")
    await _manager(hass)
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health", new=_health_version("2026.9.76")
    ), patch(_AUTO_INSTALL, new=AsyncMock()) as install:
        await init_integration(hass)
        await _publish(hass, release_check, "2026.9.77")
    install.assert_not_awaited()


async def test_turning_auto_update_all_on_installs_an_available_update(hass, release_check):
    """[KSM-TEST-152] Switching the fleet option on re-evaluates loaded devices."""
    release_check.return_value = ReleaseInfo("2026.9.77", "https://example.invalid/77", "notes")
    manager = await _manager(hass)
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health", new=_health_version("2026.9.76")
    ), patch(_AUTO_INSTALL, new=AsyncMock()) as install:
        device = await init_integration(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        install.assert_not_awaited()
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _entity(hass, manager, "auto_update_all")}, blocking=True
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    install.assert_awaited_once()
    assert install.await_args.args[1] is device.entry


async def test_auto_update_all_respects_a_skipped_version(hass, release_check):
    """[KSM-TEST-152] negative case: HA's skip wins over the fleet switch."""
    release_check.return_value = ReleaseInfo("2026.9.77", "https://example.invalid/77", "notes")
    manager = await _manager(hass)
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health", new=_health_version("2026.9.76")
    ), patch(_AUTO_INSTALL, new=AsyncMock()) as install:
        device = await init_integration(hass)
        await hass.services.async_call(
            "update", "skip", {"entity_id": _entity(hass, device.entry, "update")}, blocking=True
        )
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": _entity(hass, manager, "auto_update_all")}, blocking=True
        )
        await _publish(hass, release_check, "2026.9.77")
    install.assert_not_awaited()
