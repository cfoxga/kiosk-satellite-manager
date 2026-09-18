"""Config flow integration test (KSM-BEHAVE-001) -- drives the real
ConfigFlow through phacc's flow manager, with AdbClient mocked at the
connect/getprop boundary. Real ADB I/O is covered live, not in this suite --
see docs/SPEC/provisioning.md.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries, data_entry_flow

from custom_components.kiosk_satellite_manager.adb_client import AdbAuthPending
from custom_components.kiosk_satellite_manager.const import (
    CONF_DEVICE_PROFILE,
    CONF_HOST,
    DOMAIN,
)


async def test_user_flow_creates_entry_on_successful_connect(hass):
    # Successful CREATE_ENTRY triggers the real async_setup_entry, whose
    # coordinator does a first refresh -- fetch_health must be mocked for
    # that too, or phacc's pytest-socket blocks the real network call.
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "unknown"}),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=["tv,nosdcard", "onn"])
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.168.50.64"
    assert result["data"][CONF_DEVICE_PROFILE] == "gtv_stick"


async def test_user_flow_shows_auth_pending_error_when_device_never_confirms(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.config_flow.CONNECT_RETRY_DELAY_S", 0
    ):
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock(side_effect=AdbAuthPending("never tapped"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.99", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "auth_pending"
