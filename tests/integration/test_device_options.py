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
    rename.assert_awaited_once_with(device.entry_id, "New Display", allow_adb=True)
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


PIN = "ab" * 32
NEW_HOST = "192.0.2.99"
_FETCH = "custom_components.kiosk_satellite_manager.config_flow.fetch_health"


async def _open_host_step(hass, surface):
    """KSM-BEHAVE-130: Change host on a per-device entry or a native subentry.
    Returns (configure, flow_id, host_reader, data_reader)."""
    from homeassistant.config_entries import ConfigSubentry, SOURCE_RECONFIGURE
    from types import MappingProxyType
    from custom_components.kiosk_satellite_manager import fleet
    from .test_global_settings import _manager

    data = {CONF_HOST: "old.example.test", CONF_NAME: "Display", CONF_PASSWORD: "secret",
            "tls_spki_sha256": PIN}
    if surface == "entry":
        entry = MockConfigEntry(domain=DOMAIN, title="Display", unique_id="old.example.test",
                                data=data, options={CONF_AUTO_UPDATE: True})
        entry.add_to_hass(hass)
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        manager, read = hass.config_entries.options, lambda: dict(entry.data)
    else:
        await _manager(hass)
        parent = fleet.unmanaged_entry(hass)
        hass.config_entries.async_add_subentry(parent, ConfigSubentry(
            data=MappingProxyType({**data, "port": 5555, "key_path": "/tmp/adbkey"}),
            subentry_id="host-sub", subentry_type="device", title="Display",
            unique_id="host-sub",
        ))
        flow = await hass.config_entries.subentries.async_init(
            (parent.entry_id, "device"),
            context={"source": SOURCE_RECONFIGURE, "subentry_id": "host-sub"},
        )
        manager = hass.config_entries.subentries
        read = lambda: dict(parent.subentries["host-sub"].data)
    assert flow["type"] == data_entry_flow.FlowResultType.MENU
    assert "device_host" in flow["menu_options"]
    form = await manager.async_configure(flow["flow_id"], {"next_step_id": "device_host"})
    assert form["type"] == data_entry_flow.FlowResultType.FORM
    return manager, flow["flow_id"], read


@pytest.mark.parametrize("surface", ["entry", "subentry"])
async def test_change_host_probes_under_pin_then_updates_only_host(hass, surface):
    """[KSM-TEST-249] A reachable new host is probed with the entry's pin, then only host changes."""
    with patch(_FETCH, new=AsyncMock(return_value={"appVersion": "2026.9.90"})) as probe:
        manager, flow_id, read = await _open_host_step(hass, surface)
        before = read()
        assert before[CONF_HOST] == "old.example.test"
        probe.assert_not_awaited()
        result = await manager.async_configure(flow_id, {CONF_HOST: f"  {NEW_HOST} "})
    probe.assert_awaited_once()
    assert probe.await_args.args[1] == NEW_HOST
    assert probe.await_args.kwargs == {"pin": PIN}
    assert result["type"] in (
        data_entry_flow.FlowResultType.CREATE_ENTRY, data_entry_flow.FlowResultType.ABORT
    )
    after = read()
    assert after[CONF_HOST] == NEW_HOST
    assert {k: v for k, v in after.items() if k != CONF_HOST} == {
        k: v for k, v in before.items() if k != CONF_HOST
    }
    assert "secret" not in str(result)


@pytest.mark.parametrize("surface", ["entry", "subentry"])
@pytest.mark.parametrize("candidate,error,expected", [
    (NEW_HOST, "mismatch", "host_key_mismatch"),
    (NEW_HOST, "down", "cannot_connect"),
    (NEW_HOST, "timeout", "cannot_connect"),
    ("", None, "invalid_host"),
    ("https://192.0.2.99", None, "invalid_host"),
    ("192.0.2.99/path", None, "invalid_host"),
    ("two words", None, "invalid_host"),
])
async def test_change_host_refusals_write_nothing(hass, surface, candidate, error, expected):
    """[KSM-TEST-250] Key mismatch, unreachable and malformed hosts are refused with no write."""
    import aiohttp
    exc = {
        "mismatch": aiohttp.ServerFingerprintMismatch(
            b"\x00" * 32, b"\x01" * 32, NEW_HOST, 2324),
        "down": aiohttp.ClientConnectionError("refused"),
        "timeout": TimeoutError(),
        None: AssertionError("probe must not run for a malformed host"),
    }[error]
    with patch(_FETCH, new=AsyncMock(side_effect=exc)) as probe:
        manager, flow_id, read = await _open_host_step(hass, surface)
        before = read()
        result = await manager.async_configure(flow_id, {CONF_HOST: candidate})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"][CONF_HOST] == expected
    assert (probe.await_count == 1) == (error is not None)
    assert read() == before


async def _open_launcher_step(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "device_launcher" in result["menu_options"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device_launcher"}
    )


@pytest.mark.parametrize(
    ("profile", "expected_default"),
    [("portal_go", True), ("onn_4k_pro_android14", False)],
)
async def test_KSM_TEST_282_launcher_step_defaults_from_recipe_and_stores_choice(
    hass, profile, expected_default
):
    """[KSM-TEST-282] The Configure menu offers Replace launcher, defaulting
    from the recipe, and a submitted choice is stored on the device entry."""
    from custom_components.kiosk_satellite_manager.const import CONF_DEVICE_PROFILE

    entry = MockConfigEntry(
        domain=DOMAIN, title="Display", unique_id="192.0.2.42",
        data={CONF_HOST: "192.0.2.42", CONF_NAME: "Display", CONF_PASSWORD: "x",
              CONF_DEVICE_PROFILE: profile},
    )
    entry.add_to_hass(hass)
    result = await _open_launcher_step(hass, entry)
    assert result["step_id"] == "device_launcher"
    field = next(k for k in result["data_schema"].schema if k == "replace_launcher")
    assert field.default() is expected_default

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"replace_launcher": not expected_default}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data["replace_launcher"] is (not expected_default)
    assert entry.data[CONF_PASSWORD] == "x"


async def test_KSM_TEST_282_subentry_launcher_choice_is_stored(hass):
    """[KSM-TEST-282] A native subentry stores Replace launcher on its own data."""
    from homeassistant.config_entries import ConfigSubentry, SOURCE_RECONFIGURE
    from types import MappingProxyType
    from custom_components.kiosk_satellite_manager import fleet
    from .test_global_settings import _manager

    await _manager(hass)
    parent = fleet.unmanaged_entry(hass)
    hass.config_entries.async_add_subentry(parent, ConfigSubentry(
        data=MappingProxyType({CONF_HOST: "192.0.2.43", CONF_NAME: "Display",
                               CONF_PASSWORD: "secret", "port": 5555,
                               "key_path": "/tmp/adbkey", "device_profile": "onn_4k_pro_android14"}),
        subentry_id="launcher-sub", subentry_type="device", title="Display",
        unique_id="launcher-sub",
    ))
    flow = await hass.config_entries.subentries.async_init(
        (parent.entry_id, "device"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": "launcher-sub"},
    )
    assert "device_launcher" in flow["menu_options"]
    form = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"next_step_id": "device_launcher"}
    )
    assert form["step_id"] == "device_launcher"
    result = await hass.config_entries.subentries.async_configure(
        form["flow_id"], {"replace_launcher": False}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    data = parent.subentries["launcher-sub"].data
    assert data["replace_launcher"] is False
    assert data[CONF_PASSWORD] == "secret"


async def test_KSM_TEST_282_launcher_step_without_approved_recipe_defaults_off(hass):
    """[KSM-TEST-282] A device with no approved recipe defaults Replace launcher off."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Display", unique_id="192.0.2.44",
        data={CONF_HOST: "192.0.2.44", CONF_NAME: "Display", CONF_PASSWORD: "x",
              "device_profile": "unknown"},
    )
    entry.add_to_hass(hass)
    result = await _open_launcher_step(hass, entry)
    field = next(k for k in result["data_schema"].schema if k == "replace_launcher")
    assert field.default() is False
