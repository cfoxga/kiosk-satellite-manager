"""Per-device credential repair (KSM-BEHAVE-086)."""

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant import data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager.const import (
    CONF_AUTO_UPDATE, CONF_HOST, CONF_NAME, CONF_PASSWORD, DOMAIN,
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


async def test_device_password_update_verifies_and_preserves_other_data(hass, device):
    """[KSM-TEST-165] Successful device Configure checks the candidate first."""
    result = await hass.config_entries.options.async_init(device.entry_id)
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
    result = await hass.config_entries.options.async_init(device.entry_id)
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
