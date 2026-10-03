"""KSM-TEST-330..332 (#135): the device-support repair, its fix flow, the
support_request service and the request in Download diagnostics.

Only the ADB transport is faked: the collector, catalog resolution, request
builder, issue registry and repairs flow manager are the real ones.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest

from custom_components.kiosk_satellite_manager import diagnostics
from custom_components.kiosk_satellite_manager.const import CONF_DEVICE_PROFILE, DOMAIN

from .conftest import admin_context, init_integration

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_ADB = "custom_components.kiosk_satellite_manager.support_request.AdbClient"
_REVOKE_ENTRY = "custom_components.kiosk_satellite_manager.async_revoke_owned_credential"

_ONN_BOX = {
    "ro.product.manufacturer": "onn", "ro.product.brand": "onn",
    "ro.product.model": "onn 4K Streaming Box", "ro.product.device": "dopinder",
    "ro.build.characteristics": "tv,nosdcard", "ro.build.version.sdk": "31",
}
_ONN_PRO = {
    "ro.product.manufacturer": "onn", "ro.product.model": "onn 4K Pro Streaming Device",
    "ro.build.characteristics": "tv,nosdcard", "ro.build.version.sdk": "34",
}


def _issue_id(entry) -> str:
    return f"device_support_{entry.entry_id}"


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry))


async def _setup(hass, **data):
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        return (await init_integration(hass, data=data)).entry


def _adb(props: dict[str, str] | None = None, *, connect_error: Exception | None = None):
    """Patch support_request's AdbClient with a shell answering `props`."""
    async def shell(command: str) -> str:
        if command.startswith("getprop "):
            return (props or {}).get(command.removeprefix("getprop "), "")
        return ""

    patcher = patch(_ADB)
    client_cls = patcher.start()
    client = client_cls.return_value
    client.connect = AsyncMock(side_effect=connect_error)
    client.shell = AsyncMock(side_effect=shell)
    client.close = AsyncMock()
    return patcher, client


# --- KSM-TEST-330: the repair is raised only where it is actionable ---------

async def test_KSM_TEST_330_a_device_without_a_recipe_raises_the_repair(hass):
    entry = await _setup(hass)
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "device_support"
    assert issue.data == {"entry_id": entry.entry_id}


async def test_KSM_TEST_330_no_repair_for_a_supported_or_already_requested_device(hass):
    supported = await _setup(hass, **{CONF_DEVICE_PROFILE: "portal_go"}, host="192.168.99.98")
    requested = await _setup(hass, support_requested=True, host="192.168.99.97")
    assert _issue(hass, supported) is None
    assert _issue(hass, requested) is None
    # Control: an unknown model on the same hass does raise.
    unknown = await _setup(hass, **{CONF_DEVICE_PROFILE: "not_a_model"}, host="192.168.99.96")
    assert _issue(hass, unknown) is not None


async def test_KSM_TEST_330_removing_the_device_clears_the_repair(hass):
    entry = await _setup(hass)
    assert _issue(hass, entry) is not None
    with patch(_REVOKE_ENTRY, new=AsyncMock()):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    assert _issue(hass, entry) is None


# --- KSM-TEST-331: the fix flow --------------------------------------------

async def _start_flow(hass, entry):
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": _issue_id(entry)})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    return manager, result


async def test_KSM_TEST_331_confirm_builds_the_request_and_submit_marks_it_sent(hass):
    entry = await _setup(hass)
    manager, result = await _start_flow(hass, entry)
    patcher, client = _adb(_ONN_BOX)
    try:
        result = await manager.async_configure(result["flow_id"], {})
    finally:
        patcher.stop()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "send"
    placeholders = result["description_placeholders"]
    assert placeholders["device"] == "onn 4K Streaming Box (SDK 31)"
    assert placeholders["candidate"] == "android_tv"
    query = parse_qs(urlsplit(placeholders["url"]).query)
    assert query["template"] == ["device-support.yml"]
    client.close.assert_awaited_once()
    assert entry.data.get("support_requested") is None  # not yet sent

    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data["support_requested"] is True
    assert _issue(hass, entry) is None


async def test_KSM_TEST_331_a_device_that_is_now_supported_aborts_and_clears(hass):
    entry = await _setup(hass)
    manager, result = await _start_flow(hass, entry)
    patcher, _ = _adb(_ONN_PRO)
    try:
        result = await manager.async_configure(result["flow_id"], {})
    finally:
        patcher.stop()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "now_supported"
    assert _issue(hass, entry) is None
    assert entry.data.get("support_requested") is None


async def test_KSM_TEST_331_an_adb_failure_aborts_and_keeps_the_repair(hass):
    entry = await _setup(hass)
    manager, result = await _start_flow(hass, entry)
    patcher, client = _adb(connect_error=OSError("refused"))
    try:
        result = await manager.async_configure(result["flow_id"], {})
    finally:
        patcher.stop()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect_adb"
    assert _issue(hass, entry) is not None
    assert entry.data.get("support_requested") is None
    client.close.assert_awaited_once()


# --- KSM-TEST-332: service and diagnostics ---------------------------------

async def test_KSM_TEST_332_the_service_returns_request_and_url_and_feeds_diagnostics(hass):
    entry = await _setup(hass)
    other = await _setup(hass, host="192.168.99.95")
    patcher, client = _adb(_ONN_BOX)
    try:
        response = await hass.services.async_call(
            DOMAIN, "support_request", {"config_entry_id": entry.entry_id},
            blocking=True, return_response=True, context=await admin_context(hass),
        )
    finally:
        patcher.stop()

    assert hass.services.supports_response(DOMAIN, "support_request") is SupportsResponse.ONLY
    assert response["request"]["kind"] == "new_device"
    assert response["request"]["device_model"]["devices"] == ["dopinder"]
    assert response["url"].startswith("https://github.com/cfoxga/kiosk-satellite-manager/issues/new?")
    client.close.assert_awaited_once()

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert diag["devices"][0]["support_request"]["kind"] == "new_device"
    assert diag["devices"][0]["support_request"]["device_model"]["devices"] == ["dopinder"]
    other_diag = await diagnostics.async_get_config_entry_diagnostics(hass, other)
    assert other_diag["devices"][0]["support_request"] is None


async def test_KSM_TEST_332_the_service_rejects_an_unknown_entry(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError, match="Unknown or not active"):
        await hass.services.async_call(
            DOMAIN, "support_request", {"config_entry_id": "not-an-entry"},
            blocking=True, return_response=True, context=await admin_context(hass),
        )
