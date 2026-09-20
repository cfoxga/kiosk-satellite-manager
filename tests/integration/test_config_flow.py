"""Config flow integration test (KSM-BEHAVE-001, extended by
KSM-BEHAVE-009/012). Drives the real ConfigFlow through phacc's flow
manager, with AdbClient mocked at the connect/getprop/shell boundary. Real
ADB I/O is covered live, not in this suite -- see docs/SPEC/provisioning.md.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import voluptuous as vol

from homeassistant import config_entries, data_entry_flow

from custom_components.kiosk_satellite_manager.adb_client import AdbAuthPending
from custom_components.kiosk_satellite_manager.const import (
    CONF_AREA_ID,
    CONF_DEVICE_PROFILE,
    CONF_EXISTING_INSTALL_ACTION,
    CONF_HA_TOKEN,
    CONF_HOME_LAUNCHER,
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_TOKEN_MODE,
    DOMAIN,
    EXISTING_INSTALL_REINSTALL,
    EXISTING_INSTALL_REUSE,
    TOKEN_MODE_AUTO,
    TOKEN_MODE_MANUAL,
    TOKEN_MODE_REUSE,
)


async def test_user_flow_shows_device_info_step_with_discovered_name_default(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=["tv,nosdcard", "onn"])
        mock_client.shell = AsyncMock(return_value="Living Room TV")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "device_info"
    schema_name = next(k for k in result["data_schema"].schema if k == CONF_NAME)
    assert schema_name.default() == "Living Room TV"
    schema_pass = next(k for k in result["data_schema"].schema if k == CONF_PASSWORD)
    # KSM-TEST-006: no hardcoded default password any more.
    assert schema_pass.default is vol.UNDEFINED
    assert result["description_placeholders"]["device_model"] == "Google TV stick (onn/Chromecast-class)"


async def test_user_flow_identifies_portal_models_and_defaults_password(hass):
    """[KSM-TEST-003, KSM-TEST-005] User flow identifies Meta Portal models."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=["nosdcard", "Facebook", "PortalGo", "29"])
        mock_client.shell = AsyncMock(return_value="Test Portal Portal")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.40.224", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "device_info"
    assert result["description_placeholders"]["device_model"] == "Meta Portal Go"
    schema_pass = next(k for k in result["data_schema"].schema if k == CONF_PASSWORD)
    assert schema_pass.default is vol.UNDEFINED
    schema_name = next(k for k in result["data_schema"].schema if k == CONF_NAME)
    assert schema_name.default() == "Test Portal"


async def test_user_flow_uses_and_normalizes_portal_bluetooth_name(hass):
    """[KSM-TEST-016] Portal labels live in bluetooth_name, not device_name."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=["tablet", "facebook"])
        mock_client.shell = AsyncMock(return_value="Test Portal Portal")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.40.224", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    schema_key = next(k for k in result["data_schema"].schema if k == CONF_NAME)
    assert schema_key.default() == "Test Portal"
    mock_client.shell.assert_awaited_once_with("settings get secure bluetooth_name")



async def test_user_flow_creates_entry_after_device_info_step(hass):
    # Successful CREATE_ENTRY triggers the real async_setup_entry, whose
    # coordinator does a first refresh -- fetch_health must be mocked for
    # that too, or phacc's pytest-socket blocks the real network call.
    # install_and_launch is mocked too (KSM-BEHAVE-012): the flow now runs
    # it itself as a progress step right after device_info.
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "unknown"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        # A real asyncio.sleep(0) forces the install task to genuinely
        # suspend at least once -- a bare AsyncMock() with no real await
        # inside resolves within the same eager-task step that creates it
        # (HA's async_create_task uses eager_start), so the flow would jump
        # straight past SHOW_PROGRESS to SHOW_PROGRESS_DONE before this test
        # ever gets a chance to observe the progress step, unlike a real
        # install, which always crosses real await points (network I/O).
        async def _install_ok(*args, **kwargs):
            await asyncio.sleep(0)

        mock_install.side_effect = _install_ok
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=["tv,nosdcard", "onn"])
        mock_client.shell = AsyncMock(return_value="Living Room TV")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "device_info"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Kitchen Display",
                CONF_AREA_ID: "kitchen",
                CONF_PASSWORD: "hunter222",
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
        assert result["step_id"] == "install"

        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen Display"
    assert result["data"][CONF_HOST] == "192.168.50.64"
    assert result["data"][CONF_DEVICE_PROFILE] == "gtv_stick"
    assert result["data"][CONF_NAME] == "Kitchen Display"
    assert result["data"][CONF_AREA_ID] == "kitchen"
    assert result["data"][CONF_PASSWORD] == "hunter222"
    mock_install.assert_awaited_once()
    _, kwargs = mock_install.await_args
    assert kwargs["host"] == "192.168.50.64"
    assert kwargs["device_name"] == "Kitchen Display"
    assert kwargs["password"] == "hunter222"


async def test_user_flow_creates_entry_even_when_install_fails(hass):
    # KSM-BEHAVE-012: install is best-effort -- the device is already
    # paired by this point (the hard, human-in-the-loop part), so a failed
    # install shouldn't cost the user the whole flow. The button remains
    # the recovery path.
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "unknown"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        async def _install_fail(*args, **kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("network unreachable")

        mock_install.side_effect = _install_fail
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=["tv,nosdcard", "onn"])
        mock_client.shell = AsyncMock(return_value="null")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.77", "port": 5555}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Hallway", CONF_PASSWORD: "hunter222"},
        )
        assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS

        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_NAME] == "Hallway"


async def test_user_flow_device_info_defaults_name_to_host_when_device_name_unset(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=["tv,nosdcard", "onn"])
        mock_client.shell = AsyncMock(return_value="null")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.77", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "device_info"
    schema_key = next(k for k in result["data_schema"].schema if k == CONF_NAME)
    default_name = schema_key.default()
    assert default_name == "192.168.50.77"


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
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "auth_pending"


async def test_user_flow_manual_token(hass):
    async def _install_ok(*args, **kwargs):
        await asyncio.sleep(0)
        return "my-manual-token"

    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "unknown"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch",
        side_effect=_install_ok,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=["tv,nosdcard", "onn"])
        mock_client.shell = AsyncMock(return_value="Office Display")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.88", "port": 5555}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM

        # Validation failure when manual selected but no token provided
        result_err = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Office Display",
                CONF_PASSWORD: "admin",
                CONF_TOKEN_MODE: TOKEN_MODE_MANUAL,
                CONF_HA_TOKEN: "",
            },
        )
        assert result_err["type"] == data_entry_flow.FlowResultType.FORM
        assert result_err["errors"][CONF_HA_TOKEN] == "token_required"

        # Provide manual token
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Office Display",
                CONF_PASSWORD: "admin",
                CONF_TOKEN_MODE: TOKEN_MODE_MANUAL,
                CONF_HA_TOKEN: "my-manual-token",
                CONF_HOME_LAUNCHER: True,
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN_MODE] == TOKEN_MODE_MANUAL
    assert result["data"][CONF_HA_TOKEN] == "my-manual-token"
    assert result["data"][CONF_HOME_LAUNCHER] is True


async def test_user_flow_kept_install_collects_only_existing_connection_details(hass):
    """[KSM-TEST-021] A kept install needs only KSM entry details and the
    existing remote-UI password; it must not overwrite device settings."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.62"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=["tv,nosdcard", "onn"])
        mock_client.shell = AsyncMock(return_value="Living Room TV")
        mock_client.is_ks_installed = AsyncMock(return_value=True)
        mock_client.uninstall_ks = AsyncMock()
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        # KSM-BEHAVE-021: the package choice comes *before* device_info.
        assert result["step_id"] == "existing_install"
        action_key = next(
            k for k in result["data_schema"].schema if k == CONF_EXISTING_INSTALL_ACTION
        )
        assert action_key.default() == EXISTING_INSTALL_REUSE

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EXISTING_INSTALL_ACTION: EXISTING_INSTALL_REUSE}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "existing_device_info"
        schema_fields = {field.schema for field in result["data_schema"].schema}
        assert schema_fields == {CONF_NAME, CONF_AREA_ID, CONF_PASSWORD}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Living Room TV", CONF_PASSWORD: "hunter222"},
        )
        assert result["type"] in (
            data_entry_flow.FlowResultType.CREATE_ENTRY,
            data_entry_flow.FlowResultType.SHOW_PROGRESS,
        )
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            assert result["step_id"] == "install"
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
            await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room TV"
    assert CONF_HOME_LAUNCHER not in result["data"]
    assert CONF_HA_TOKEN not in result["data"]
    mock_install.assert_not_awaited()
    mock_client.uninstall_ks.assert_not_awaited()


async def test_user_flow_reinstall_uninstalls_before_install(hass):
    """[KSM-TEST-022] Reinstall keeps the full fresh-install form (including
    token selection) and uninstalls before installing."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.62"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        call_order = []

        async def _uninstall(*args, **kwargs):
            call_order.append("uninstall")

        async def _install_ok(*args, **kwargs):
            call_order.append("install")
            await asyncio.sleep(0)

        mock_install.side_effect = _install_ok
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=["tv,nosdcard", "onn"])
        mock_client.shell = AsyncMock(return_value="Living Room TV")
        mock_client.is_ks_installed = AsyncMock(return_value=True)
        mock_client.uninstall_ks = AsyncMock(side_effect=_uninstall)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "existing_install"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EXISTING_INSTALL_ACTION: EXISTING_INSTALL_REINSTALL},
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "device_info"
        schema_fields = {field.schema for field in result["data_schema"].schema}
        assert {CONF_HOME_LAUNCHER, CONF_TOKEN_MODE, CONF_HA_TOKEN} <= schema_fields

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Living Room TV", CONF_PASSWORD: "hunter222"},
        )
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
            await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    mock_client.uninstall_ks.assert_awaited_once()
    mock_install.assert_awaited_once()
    assert call_order == ["uninstall", "install"]
