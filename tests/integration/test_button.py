"""Install button integration test (KSM-BEHAVE-001, extended by
KSM-BEHAVE-007/008). AdbClient and the GitHub APK lookup are mocked at the
boundary -- the live ADB push/install path and the live GitHub releases API
shape are each verified separately (see docs/SPEC/provisioning.md). This
test proves the button wires the shared install_and_launch sequence
together, flags the coordinator "installing" for its duration, and refreshes
the version sensor afterward.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers import entity_registry as er

from custom_components.kiosk_satellite_manager.const import DOMAIN

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
        ctx = await init_integration(hass)

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
