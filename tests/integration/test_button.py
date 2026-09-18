"""Install button integration test (KSM-BEHAVE-001). AdbClient and the GitHub
APK lookup are mocked at the boundary -- the live ADB push/install path and
the live GitHub releases API shape are each verified separately (see
docs/SPEC/provisioning.md). This test proves the button wires them together
and refreshes the version sensor afterward.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers import entity_registry as er

from .conftest import init_integration


def _fake_apk_response():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.read = AsyncMock(return_value=b"fake-apk-bytes")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def test_press_installs_and_refreshes_version(hass):
    # fetch_health is called once by the coordinator's first refresh during
    # setup, and again by the refresh the button triggers after install --
    # both must stay mocked for the whole test, or phacc's pytest-socket
    # blocks the real network call.
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

        fake_session = MagicMock()
        fake_session.get = MagicMock(return_value=_fake_apk_response())

        with patch(
            "custom_components.kiosk_satellite_manager.button.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.button.latest_apk_url",
            new=AsyncMock(return_value="https://example.invalid/ks.apk"),
        ), patch(
            "custom_components.kiosk_satellite_manager.button.async_get_clientsession",
            return_value=fake_session,
        ):
            mock_client = mock_client_cls.return_value
            mock_client.connect = AsyncMock()
            mock_client.getprop = AsyncMock(return_value="armeabi-v7a")
            mock_client.push = AsyncMock()
            mock_client.shell = AsyncMock(return_value="")
            mock_client.close = AsyncMock()

            await hass.services.async_call(
                "button", "press", {"entity_id": button_entry.entity_id}, blocking=True
            )

    assert mock_client.push.await_count == 1
    assert hass.states.get(sensor_entry.entity_id).state == "new"
