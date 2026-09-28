"""Per-device credential repair (KSM-BEHAVE-086)."""

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager.const import (
    CONF_AUTO_UPDATE, CONF_HOST, CONF_NAME, CONF_PASSWORD, DOMAIN, RENAME_API_KEY,
)
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError


@pytest.fixture
def device(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Display", unique_id="192.0.2.41",
        data={CONF_HOST: "192.0.2.41", CONF_NAME: "Display", CONF_PASSWORD: "old-secret"},
        options={CONF_AUTO_UPDATE: True},
    )
    entry.add_to_hass(hass)
    return entry


async def _open_password_step(hass, device):
    """KSM-BEHAVE-088: device Configure opens a menu; pick the password step."""
    result = await hass.config_entries.options.async_init(device.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device_password"}
    )


async def test_device_rename_uses_shared_operation_and_reports_partial_result(hass, device):
    """[KSM-TEST-244] Configure exposes the operation and its incomplete layers."""
    result = await hass.config_entries.options.async_init(device.entry_id)
    assert "device_rename" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device_rename"}
    )
    assert result["step_id"] == "device_rename"
    name_field = next(k for k in result["data_schema"].schema if k == CONF_NAME)
    assert name_field.default() == "Display"

    rename = AsyncMock(return_value={
        "ks": "applied", "android": "unsupported", "entry": "applied",
        "host": "pending", "esphome": "applied",
        "esphome_actions": {"callers": ["automation.old_display"], "removed": []},
    })
    hass.data[RENAME_API_KEY] = rename
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "New Display"}
    )
    rename.assert_awaited_once_with(device.entry_id, "New Display")
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "device_rename_incomplete"
    summary = result["description_placeholders"]["result"]
    assert "host: pending" in summary
    assert "android: unsupported" in summary
    assert "automation.old_display" in summary


@pytest.mark.parametrize("name", ["", "!!!"])
async def test_device_rename_rejects_invalid_names_before_io(hass, device, name):
    """[KSM-TEST-244] Invalid names leave the form open with no device call."""
    result = await hass.config_entries.options.async_init(device.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device_rename"}
    )
    rename = AsyncMock()
    hass.data[RENAME_API_KEY] = rename
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: name}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"][CONF_NAME] == "invalid_device_name"
    rename.assert_not_awaited()


async def test_device_rename_unavailable_does_not_claim_success(hass, device):
    """[KSM-TEST-244] An unloaded integration cannot report a rename."""
    result = await hass.config_entries.options.async_init(device.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device_rename"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "New Display"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "device_rename_unavailable"
    assert device.title == "Display"


async def test_device_rename_validation_failure_stays_on_form(hass, device):
    """[KSM-TEST-244] A shared-operation rejection is visible without success."""
    result = await hass.config_entries.options.async_init(device.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device_rename"}
    )
    rename = AsyncMock(side_effect=ServiceValidationError("no password"))
    hass.data[RENAME_API_KEY] = rename
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "New Display"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "device_rename_failed"
    assert "no password" not in str(result)


async def test_device_password_update_verifies_and_preserves_other_data(hass, device):
    """[KSM-TEST-165] Successful device Configure checks the candidate first."""
    result = await _open_password_step(hass, device)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    password_field = next(k for k in result["data_schema"].schema if k == CONF_PASSWORD)
    assert password_field.default is vol.UNDEFINED
    assert "old-secret" not in str(result)

    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.login",
        new=AsyncMock(return_value="device-token"),
    ) as login:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-secret"}
        )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    login.assert_awaited_once()
    assert login.await_args.args[1:] == ("192.0.2.41", "new-secret")
    assert device.data == {
        CONF_HOST: "192.0.2.41", CONF_NAME: "Display", CONF_PASSWORD: "new-secret"
    }
    assert device.options == {CONF_AUTO_UPDATE: True}
    assert "new-secret" not in str(result)


@pytest.mark.parametrize("candidate,error", [
    ("", None),
    ("incorrect-secret", KsApiError("invalid password")),
    ("unreachable-secret", OSError("network down")),
    ("redirected-secret", KsApiError("HTTP 302")),
])
async def test_device_password_update_rejects_without_writing(
    hass, device, candidate, error
):
    """[KSM-TEST-166] Failed validation never replaces the working secret."""
    result = await _open_password_step(hass, device)
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.login",
        new=AsyncMock(side_effect=error),
    ) as login:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_PASSWORD: candidate}
        )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]
    assert device.data[CONF_PASSWORD] == "old-secret"
    assert device.options == {CONF_AUTO_UPDATE: True}
    if candidate:
        login.assert_awaited_once()
    else:
        login.assert_not_awaited()
    if candidate:
        assert candidate not in str(result)
    assert "invalid password" not in str(result)
