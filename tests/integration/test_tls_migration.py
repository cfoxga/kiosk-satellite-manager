"""No HTTPS migration at setup, the certificate-changed repair and the
HTTPS-turned-off repair (KSM-BEHAVE-094/095/170, #57, #137), against a real
hass via phacc."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.kiosk_satellite_manager.const import CONF_TLS_SPKI, DOMAIN

from .conftest import init_integration

PIN = "ab" * 32
NEW_PIN = "cd" * 32
HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
ESTABLISH = "custom_components.kiosk_satellite_manager.ks_tls.async_establish_tls"
PROBE = "custom_components.kiosk_satellite_manager.ks_api_client.probe_https"
PATCH_SETTINGS = "custom_components.kiosk_satellite_manager.ks_api_client.patch_settings"
GET_HEALTH = "custom_components.kiosk_satellite_manager.ks_api_client.get_health"


def _mismatch() -> aiohttp.ServerFingerprintMismatch:
    return aiohttp.ServerFingerprintMismatch(bytes.fromhex(PIN), bytes.fromhex(NEW_PIN), "192.168.99.99", 2324)


async def test_setup_never_switches_an_unpinned_entry_to_https(hass):
    """[KSM-TEST-336] #137: setup of an unpinned entry with a password sends
    no remote.tls and leaves it unpinned; a pinned entry keeps its pin."""
    establish = AsyncMock(return_value=PIN)
    patch_settings = AsyncMock(return_value={"ok": True})
    with patch(HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.84"})), patch(
        ESTABLISH, new=establish
    ), patch(PATCH_SETTINGS, new=patch_settings):
        ctx = await init_integration(hass)
        pinned = await init_integration(hass, data={"host": "192.168.99.98", CONF_TLS_SPKI: PIN})
        await hass.async_block_till_done(wait_background_tasks=True)
    establish.assert_not_awaited()
    assert not [c for c in patch_settings.await_args_list if "remote.tls" in c.args[3]]
    assert CONF_TLS_SPKI not in ctx.entry.data
    assert pinned.entry.data[CONF_TLS_SPKI] == PIN


async def test_pin_mismatch_raises_the_certificate_changed_repair(hass):
    """[KSM-TEST-183] A key mismatch on the pinned health read raises the
    repair; an ordinary connection failure does not."""
    with patch(HEALTH, new=AsyncMock(side_effect=aiohttp.ClientConnectionError("refused"))):
        ctx = await init_integration(hass, data={CONF_TLS_SPKI: PIN})
    issue_id = f"tls_certificate_changed_{ctx.entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
    health = AsyncMock(side_effect=_mismatch())
    with patch(HEALTH, new=health):
        await coordinator.async_refresh()
    assert health.await_args.kwargs["pin"] == PIN
    assert coordinator.last_update_success is False
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable


async def _raise_repair(hass):
    assert await async_setup_component(hass, "repairs", {})
    with patch(HEALTH, new=AsyncMock(side_effect=_mismatch())):
        ctx = await init_integration(hass, data={CONF_TLS_SPKI: PIN})
    issue_id = f"tls_certificate_changed_{ctx.entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    return ctx, issue_id


async def test_confirming_the_repair_repins_to_the_served_key(hass):
    """[KSM-TEST-184] Confirm stores the newly served SPKI and clears the issue."""
    ctx, issue_id = await _raise_repair(hass)
    manager = repairs_flow_manager(hass)
    health = AsyncMock(return_value={"appVersion": "2026.9.84"})
    with patch(PROBE, new=AsyncMock(return_value=(NEW_PIN, {}))), patch(HEALTH, new=health):
        result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
        assert result["type"] is FlowResultType.FORM
        result = await manager.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert ctx.entry.data[CONF_TLS_SPKI] == NEW_PIN
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    # The health poll resumes on the new pin straight away.
    assert health.await_args.kwargs["pin"] == NEW_PIN
    assert hass.data[DOMAIN][ctx.entry.entry_id].last_update_success is True


async def test_repair_without_an_https_answer_keeps_the_old_pin(hass):
    """[KSM-TEST-184] Negative: no HTTPS answer aborts, pin and issue kept."""
    ctx, issue_id = await _raise_repair(hass)
    manager = repairs_flow_manager(hass)
    with patch(PROBE, new=AsyncMock(return_value=None)):
        result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
        result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    assert ctx.entry.data[CONF_TLS_SPKI] == PIN
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


def _transport(*, http_answers: bool, pinned_error: Exception | None = None):
    """A device that refuses the pinned HTTPS read and, when `http_answers`,
    serves plain HTTP health (Use HTTPS turned off in Kiosk Satellite)."""
    calls: list = []

    async def health(session, host, *, pin):
        calls.append(pin)
        if pin:
            raise pinned_error or aiohttp.ClientOSError("wrong version number")
        if http_answers:
            return {"appVersion": "2026.9.90"}
        raise aiohttp.ClientConnectionError("refused")

    return health, calls


async def test_https_turned_off_on_the_device_raises_a_repair(hass):
    """[KSM-TEST-338] A pinned read failing while HTTP health answers raises
    the fixable tls_disabled repair and keeps the pin; a good pinned read
    later clears it."""
    health, calls = _transport(http_answers=True)
    with patch(HEALTH, new=health):
        ctx = await init_integration(hass, data={CONF_TLS_SPKI: PIN})
    issue_id = f"tls_disabled_{ctx.entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable
    assert calls == [PIN, None]
    assert ctx.entry.data[CONF_TLS_SPKI] == PIN
    coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
    assert coordinator.last_update_success is False
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"tls_certificate_changed_{ctx.entry.entry_id}") is None

    with patch(HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.90"})):
        await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_offline_or_unpinned_device_raises_no_https_repair(hass):
    """[KSM-TEST-338] Negative: the same pinned failure with no HTTP answer
    (device offline) raises no repair, and an unpinned entry never probes."""
    health, calls = _transport(http_answers=False)
    with patch(HEALTH, new=health):
        pinned = await init_integration(hass, data={CONF_TLS_SPKI: PIN})
    assert calls == [PIN, None]
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"tls_disabled_{pinned.entry.entry_id}") is None

    calls.clear()
    unpinned_health = AsyncMock(side_effect=aiohttp.ClientConnectionError("refused"))
    with patch(HEALTH, new=unpinned_health):
        unpinned = await init_integration(hass, data={"host": "192.168.99.97"})
    assert unpinned_health.await_count == 1
    assert unpinned_health.await_args.kwargs["pin"] is None
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"tls_disabled_{unpinned.entry.entry_id}") is None


async def _raise_https_off_repair(hass):
    assert await async_setup_component(hass, "repairs", {})
    health, _calls = _transport(http_answers=True)
    with patch(HEALTH, new=health):
        ctx = await init_integration(hass, data={CONF_TLS_SPKI: PIN})
    issue_id = f"tls_disabled_{ctx.entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    return ctx, issue_id


async def test_confirming_the_https_off_repair_clears_the_pin(hass):
    """[KSM-TEST-339] Confirming while HTTP answers removes the pin and the
    issue; the next health read is unpinned."""
    ctx, issue_id = await _raise_https_off_repair(hass)
    manager = repairs_flow_manager(hass)
    http = AsyncMock(return_value={"appVersion": "2026.9.90"})
    health = AsyncMock(return_value={"appVersion": "2026.9.90"})
    with patch(GET_HEALTH, new=http), patch(HEALTH, new=health):
        result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
        assert result["type"] is FlowResultType.FORM
        result = await manager.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert http.await_args.kwargs["pin"] is None
    assert CONF_TLS_SPKI not in ctx.entry.data
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert health.await_args.kwargs["pin"] is None
    assert hass.data[DOMAIN][ctx.entry.entry_id].last_update_success is True


async def test_https_off_repair_without_an_http_answer_keeps_the_pin(hass):
    """[KSM-TEST-339] Negative: no HTTP answer aborts cannot_connect and
    keeps both the pin and the issue."""
    ctx, issue_id = await _raise_https_off_repair(hass)
    manager = repairs_flow_manager(hass)
    with patch(GET_HEALTH, new=AsyncMock(side_effect=aiohttp.ClientConnectionError("down"))):
        result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
        result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    assert ctx.entry.data[CONF_TLS_SPKI] == PIN
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
