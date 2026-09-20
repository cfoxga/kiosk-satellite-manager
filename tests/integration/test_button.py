"""Install button integration test (KSM-BEHAVE-001, extended by
KSM-BEHAVE-007/008). AdbClient and the GitHub APK lookup are mocked at the
boundary -- the live ADB push/install path and the live GitHub releases API
shape are each verified separately (see docs/SPEC/provisioning.md). This
test proves the button wires the shared install_and_launch sequence
together, flags the coordinator "installing" for its duration, and refreshes
the version sensor afterward.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers import entity_registry as er

from custom_components.kiosk_satellite_manager.const import (
    CONF_DEVICE_PROFILE,
    CONF_HA_TOKEN,
    DOMAIN,
    INSTALL_LAUNCH_POLL_ATTEMPTS,
)
from custom_components.kiosk_satellite_manager.credentials import TokenCredential
from custom_components.kiosk_satellite_manager.device_catalog import NoApprovedRecipe

from .conftest import init_integration


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


async def test_press_installs_launches_grants_and_refreshes_version(hass):
    # fetch_health is called once by the coordinator's first refresh during
    # setup, and again by the bounded post-install poll -- both must stay
    # mocked for the whole test, or phacc's pytest-socket blocks the real
    # network call.
    health_responses = iter([{"appVersion": "old"}, {"appVersion": "new"}])

    async def fake_fetch_health(session, host):
        return next(health_responses)

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        # The entry stores the exact device model the config flow matched
        # (issue #20); the button passes it straight to install_and_launch,
        # which resolves the approved recipe from it.
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_go"})

        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
        button_entry = next(e for e in entries if e.domain == "button")
        sensor_entry = next(e for e in entries if e.domain == "sensor")
        assert hass.states.get(sensor_entry.entity_id).state == "old"

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
            new=AsyncMock(return_value=("https://example.invalid/ks.apk", "new")),
        ), patch(
            "custom_components.kiosk_satellite_manager.install.verify_ks_apk_signer"
        ), patch(
            "custom_components.kiosk_satellite_manager.button.async_get_clientsession",
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

            await hass.services.async_call(
                "button", "press", {"entity_id": button_entry.entity_id}, blocking=True
            )

    assert mock_client.push.await_count == 1
    mock_client.install_apk.assert_awaited_once()
    shell_calls = [c.args[0] for c in mock_client.shell.await_args_list]
    assert shell_calls[1] == "am start -n me.jxl.kiosk_satellite/.MainActivity"
    assert "dumpsys deviceidle whitelist +me.jxl.kiosk_satellite" in shell_calls
    assert "appops set me.jxl.kiosk_satellite SYSTEM_ALERT_WINDOW allow" in shell_calls
    assert seen_installing_during_press is True
    assert coordinator.ksm_installing is False
    assert hass.states.get(sensor_entry.entity_id).state == "new"


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
    async def fake_fetch_health(session, host):
        return {"appVersion": "old"}

    entry_data = {}
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


async def test_press_retries_health_until_success_without_a_terminal_delay(hass):
    """[KSM-TEST-088] Post-install health polling stops at the first healthy
    response; a permanently unhealthy device gets exactly the bounded delays
    *between* attempts, never an extra delay after the final request."""
    async def fake_fetch_health(session, host):
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
    async def fake_fetch_health(session, host):
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
    async def fake_fetch_health(session, host):
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
    async def fake_fetch_health(session, host):
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
    async def fake_fetch_health(session, host):
        return {"appVersion": "old"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass)
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
    async def fake_fetch_health(session, host):
        return {"appVersion": "2026.9.62"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass)

        ent_reg = er.async_get(hass)
        entries = er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
        assert len([e for e in entries if e.domain == "button"]) == 2
        uninstall_entry = next(
            e for e in entries if e.unique_id == f"{ctx.entry.entry_id}_uninstall"
        )
        assert hass.states.get(uninstall_entry.entity_id).name.endswith(
            "Uninstall Kiosk Satellite"
        )

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
    async def fake_fetch_health(session, host):
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
