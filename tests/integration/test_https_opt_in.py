"""Use HTTPS: the per-device opt-in and the switch back to HTTP
(KSM-BEHAVE-169, #137), on a per-device entry and a native subentry."""
from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import fleet, ks_tls
from custom_components.kiosk_satellite_manager.const import (
    CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_TLS_SPKI, DOMAIN,
)
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

from .test_global_settings import _manager

PIN = "ab" * 32
HOST = "192.0.2.61"
_API = "custom_components.kiosk_satellite_manager.ks_api_client"


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch):
    monkeypatch.setattr(ks_tls, "TLS_ENABLE_POLL_DELAY_S", 0)


async def _open(hass, surface, *, pinned: bool, password: str | None = "secret"):
    """Open Configure -> Use HTTPS. Returns (manager, flow_id, data reader, form)."""
    data = {CONF_HOST: HOST, CONF_NAME: "Display", CONF_PASSWORD: password}
    if pinned:
        data[CONF_TLS_SPKI] = PIN
    if surface == "entry":
        entry = MockConfigEntry(domain=DOMAIN, title="Display", unique_id=HOST, data=data)
        entry.add_to_hass(hass)
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        manager, read = hass.config_entries.options, lambda: dict(entry.data)
    else:
        await _manager(hass)
        parent = fleet.unmanaged_entry(hass)
        hass.config_entries.async_add_subentry(parent, ConfigSubentry(
            data=MappingProxyType({**data, "port": 5555, "key_path": "/tmp/adbkey"}),
            subentry_id="https-sub", subentry_type="device", title="Display",
            unique_id="https-sub",
        ))
        flow = await hass.config_entries.subentries.async_init(
            (parent.entry_id, "device"),
            context={"source": SOURCE_RECONFIGURE, "subentry_id": "https-sub"},
        )
        manager = hass.config_entries.subentries
        read = lambda: dict(parent.subentries["https-sub"].data)
    assert flow["type"] == data_entry_flow.FlowResultType.MENU
    assert "device_https" in flow["menu_options"]
    form = await manager.async_configure(flow["flow_id"], {"next_step_id": "device_https"})
    return manager, flow["flow_id"], read, form


@pytest.mark.parametrize("surface", ["entry", "subentry"])
async def test_use_https_enables_and_pins_only_on_confirm(hass, surface, tls_migration):
    """[KSM-TEST-337] Unpinned: the step explains first, then confirming
    establishes HTTPS and stores the returned pin."""
    tls_migration.return_value = PIN
    manager, flow_id, read, form = await _open(hass, surface, pinned=False)
    assert form["type"] == data_entry_flow.FlowResultType.FORM
    assert form["step_id"] == "device_https_enable"
    tls_migration.assert_not_awaited()

    result = await manager.async_configure(flow_id, {"confirm": False})
    assert result["errors"] == {"confirm": "confirm_required"}
    tls_migration.assert_not_awaited()
    assert CONF_TLS_SPKI not in read()

    result = await manager.async_configure(flow_id, {"confirm": True})
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "https_enabled"
    tls_migration.assert_awaited_once()
    assert tls_migration.await_args.args[1:] == (HOST, "secret")
    assert read()[CONF_TLS_SPKI] == PIN
    assert "secret" not in str(result)


@pytest.mark.parametrize("surface", ["entry", "subentry"])
@pytest.mark.parametrize("outcome,reason", [
    (None, "https_unsupported"),
    (KsApiError("did not answer over HTTPS"), "https_failed"),
])
async def test_use_https_failure_stores_no_pin(hass, surface, tls_migration, outcome, reason):
    """[KSM-TEST-337] Negative: an old KS (None) or an error stores no pin."""
    if isinstance(outcome, Exception):
        tls_migration.side_effect = outcome
    else:
        tls_migration.return_value = outcome
    manager, flow_id, read, _form = await _open(hass, surface, pinned=False)
    result = await manager.async_configure(flow_id, {"confirm": True})
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == reason
    assert CONF_TLS_SPKI not in read()


@pytest.mark.parametrize("surface", ["entry", "subentry"])
async def test_switch_back_to_http_disables_over_the_pin_then_unpins(
    hass, surface, tls_migration
):
    """[KSM-TEST-337] Pinned: confirming logs in over the pinned channel,
    sends remote.tls=false there, waits for HTTP health, then removes the pin
    and the device's TLS repairs."""
    login = AsyncMock(return_value="tok")
    patch_settings = AsyncMock(return_value={"ok": True})
    http = AsyncMock(return_value={"appVersion": "2026.9.90"})
    manager, flow_id, read, form = await _open(hass, surface, pinned=True)
    assert form["step_id"] == "device_https_disable"
    device_id = "https-sub" if surface == "subentry" else next(
        e.entry_id for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == HOST)
    for issue_id in (f"tls_disabled_{device_id}", f"tls_certificate_changed_{device_id}"):
        ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=True,
                              severity=ir.IssueSeverity.ERROR, translation_key="tls_disabled",
                              translation_placeholders={"name": "Display", "host": HOST},
                              data={"entry_id": device_id})
    with patch(f"{_API}.login", new=login), patch(
        f"{_API}.patch_settings", new=patch_settings
    ), patch(f"{_API}.get_health", new=http):
        result = await manager.async_configure(flow_id, {"confirm": True})
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "https_disabled"
    assert login.await_args.args[1:] == (HOST, "secret")
    assert login.await_args.kwargs == {"pin": PIN}
    assert patch_settings.await_args.args[3] == {"remote.tls": False}
    assert patch_settings.await_args.kwargs == {"pin": PIN}
    assert http.await_args.kwargs == {"pin": None}
    assert CONF_TLS_SPKI not in read()
    assert read()[CONF_PASSWORD] == "secret"
    tls_migration.assert_not_awaited()
    for issue_id in (f"tls_disabled_{device_id}", f"tls_certificate_changed_{device_id}"):
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize("surface", ["entry", "subentry"])
@pytest.mark.parametrize("failure", ["login", "http_never"])
async def test_switch_back_failure_keeps_the_pin(hass, surface, failure):
    """[KSM-TEST-337] Negative: a refused login, or HTTP that never answers
    after the PATCH, keeps the pin -- KSM never drops to HTTP while the
    device may still serve HTTPS."""
    import aiohttp

    login = AsyncMock(side_effect=KsApiError("invalid password") if failure == "login" else None,
                      return_value="tok")
    patch_settings = AsyncMock(return_value={"ok": True})
    http = AsyncMock(side_effect=aiohttp.ClientConnectionError("refused"))
    manager, flow_id, read, _form = await _open(hass, surface, pinned=True)
    with patch(f"{_API}.login", new=login), patch(
        f"{_API}.patch_settings", new=patch_settings
    ), patch(f"{_API}.get_health", new=http):
        result = await manager.async_configure(flow_id, {"confirm": True})
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "https_failed"
    assert read()[CONF_TLS_SPKI] == PIN
    if failure == "login":
        patch_settings.assert_not_awaited()


@pytest.mark.parametrize("pinned", [False, True])
async def test_use_https_without_a_stored_password_aborts(hass, pinned, tls_migration):
    """[KSM-TEST-337] Negative: no stored password -> nothing to log in with."""
    manager, flow_id, read, form = await _open(hass, "entry", pinned=pinned, password=None)
    assert form["type"] == data_entry_flow.FlowResultType.ABORT
    assert form["reason"] == "https_password_required"
    tls_migration.assert_not_awaited()
    assert (CONF_TLS_SPKI in read()) is pinned
