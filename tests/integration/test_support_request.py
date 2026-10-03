"""KSM-TEST-330..332 (#135): the device-support repair, its fix flow, the
support_request service and the request in Download diagnostics.
KSM-TEST-345 (#141): setup resolves a device saved with no model first.

Only the ADB transport is faked: the collector, catalog resolution, request
builder, issue registry and repairs flow manager are the real ones.
"""
from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest

from custom_components.kiosk_satellite_manager import diagnostics, fleet
from custom_components.kiosk_satellite_manager.adb_client import AdbConnectFailed
from custom_components.kiosk_satellite_manager.const import CONF_DEVICE_PROFILE, DOMAIN

from .conftest import admin_context, init_integration
from .test_global_settings import _manager

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_ADB = "custom_components.kiosk_satellite_manager.support_request.AdbClient"
_SETUP_ADB = "custom_components.kiosk_satellite_manager.device_repairs.AdbClient"
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


async def _setup(hass, *, identity: dict[str, str] | None = _ONN_BOX, **data):
    """Set up one device; setup's no-model read (KSM-BEHAVE-173) sees
    `identity`, by default a TV that matches no library model. None keeps
    the conftest default, an ADB connect failure."""
    patcher = _adb(identity, target=_SETUP_ADB)[0] if identity is not None else None
    try:
        with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
            entry = (await init_integration(hass, data=data)).entry
        await hass.async_block_till_done(wait_background_tasks=True)
    finally:
        if patcher is not None:
            patcher.stop()
    return entry


def _adb(
    props: dict[str, str] | None = None,
    *,
    connect_error: Exception | None = None,
    target: str = _ADB,
):
    """Patch `target`'s AdbClient with a shell answering `props`."""
    async def shell(command: str) -> str:
        if command.startswith("getprop "):
            return (props or {}).get(command.removeprefix("getprop "), "")
        return ""

    async def getprop(prop: str) -> str:
        return (await shell(f"getprop {prop}")).strip()

    patcher = patch(target)
    client_cls = patcher.start()
    client = client_cls.return_value
    client.connect = AsyncMock(side_effect=connect_error)
    client.shell = AsyncMock(side_effect=shell)
    client.getprop = AsyncMock(side_effect=getprop)
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


# --- KSM-TEST-345: setup resolves a device saved with no model ------------

async def test_KSM_TEST_345_a_no_model_device_that_is_a_library_model_stores_it(hass):
    entry = await _setup(hass, identity=_ONN_PRO)
    assert entry.data[CONF_DEVICE_PROFILE] == "onn_4k_pro_android14"
    assert _issue(hass, entry) is None
    # Control: the same setup with an unmatched TV does raise.
    other = await _setup(hass, host="192.168.99.95")
    assert other.data.get(CONF_DEVICE_PROFILE) is None
    assert _issue(hass, other) is not None

    # The stored model is never re-read.
    patcher, client = _adb(_ONN_BOX, target=_SETUP_ADB)
    try:
        with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done(wait_background_tasks=True)
        client.connect.assert_not_awaited()
    finally:
        patcher.stop()
    assert entry.data[CONF_DEVICE_PROFILE] == "onn_4k_pro_android14"
    assert _issue(hass, entry) is None


async def test_KSM_TEST_345_a_fleet_subentry_with_no_model_stores_it(hass):
    """The prod shape: a migrated fleet subentry that was saved with no model."""
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    hass.config_entries.async_add_subentry(unmanaged, ConfigSubentry(
        data=MappingProxyType({"host": "192.168.99.72", "port": 5555,
                               "key_path": "/tmp/adbkey", "password": "",
                               "device_profile": None}),
        subentry_id="gtv-ha", subentry_type="device", title="Great Room GTV",
        unique_id="gtv-ha",
    ))
    patcher, client = _adb(_ONN_PRO, target=_SETUP_ADB)
    try:
        with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
            assert await hass.config_entries.async_reload(unmanaged.entry_id)
            await hass.async_block_till_done(wait_background_tasks=True)
    finally:
        patcher.stop()
    assert unmanaged.subentries["gtv-ha"].data[CONF_DEVICE_PROFILE] == "onn_4k_pro_android14"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "device_support_gtv-ha") is None
    client.close.assert_awaited()


async def test_KSM_TEST_345_an_unreachable_device_raises_nothing_and_stores_nothing(hass):
    entry = await _setup(hass, identity=None)  # the conftest default: connect fails
    assert entry.data.get(CONF_DEVICE_PROFILE) is None
    assert _issue(hass, entry) is None
    patcher, client = _adb(connect_error=AdbConnectFailed("offline"), target=_SETUP_ADB)
    try:
        with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done(wait_background_tasks=True)
    finally:
        patcher.stop()
    # The next setup reads it again, and still neither raises nor stores.
    client.connect.assert_awaited()
    assert entry.data.get(CONF_DEVICE_PROFILE) is None
    assert _issue(hass, entry) is None


async def test_KSM_TEST_345_a_stored_or_requested_device_opens_no_adb(hass):
    patcher, client = _adb(_ONN_PRO, target=_SETUP_ADB)
    try:
        with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
            unknown = (await init_integration(
                hass, data={CONF_DEVICE_PROFILE: "not_a_model", "host": "192.168.99.94"})).entry
            requested = (await init_integration(
                hass, data={"support_requested": True, "host": "192.168.99.93"})).entry
        await hass.async_block_till_done(wait_background_tasks=True)
        client.connect.assert_not_awaited()
    finally:
        patcher.stop()
    assert unknown.data[CONF_DEVICE_PROFILE] == "not_a_model"
    assert _issue(hass, unknown) is not None
    assert requested.data.get(CONF_DEVICE_PROFILE) is None
    assert _issue(hass, requested) is None


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
