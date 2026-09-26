"""HTTPS migration at setup and the certificate-changed repair
(KSM-BEHAVE-094/095, #57), against a real hass via phacc."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.kiosk_satellite_manager.const import CONF_PASSWORD, CONF_TLS_SPKI, DOMAIN

from .conftest import init_integration

PIN = "ab" * 32
NEW_PIN = "cd" * 32
HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
ESTABLISH = "custom_components.kiosk_satellite_manager.ks_tls.async_establish_tls"
PROBE = "custom_components.kiosk_satellite_manager.ks_api_client.probe_https"


def _mismatch() -> aiohttp.ServerFingerprintMismatch:
    return aiohttp.ServerFingerprintMismatch(bytes.fromhex(PIN), bytes.fromhex(NEW_PIN), "192.168.99.99", 2324)


async def test_setup_migrates_an_unpinned_entry_and_persists_the_pin(hass):
    """[KSM-TEST-182] An unpinned entry with a password is switched and pinned."""
    establish = AsyncMock(return_value=PIN)
    with patch(HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.84"})), patch(
        ESTABLISH, new=establish
    ):
        ctx = await init_integration(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    establish.assert_awaited_once()
    assert establish.await_args.args[1:] == ("192.168.99.99", "synthetic-test-password")
    assert ctx.entry.data[CONF_TLS_SPKI] == PIN


async def test_setup_leaves_pinned_passwordless_and_old_entries_unchanged(hass):
    """[KSM-TEST-182] Pinned and password-less entries never migrate; a None
    result (KS without TLS) and an error both leave the data untouched."""
    establish = AsyncMock(return_value=NEW_PIN)
    with patch(HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.84"})), patch(
        ESTABLISH, new=establish
    ):
        pinned = await init_integration(hass, data={CONF_TLS_SPKI: PIN})
        no_pw = await init_integration(hass, data={"host": "192.168.99.98", CONF_PASSWORD: None})
        await hass.async_block_till_done(wait_background_tasks=True)
    establish.assert_not_awaited()
    assert pinned.entry.data[CONF_TLS_SPKI] == PIN
    assert CONF_TLS_SPKI not in no_pw.entry.data

    outcomes = (AsyncMock(return_value=None), AsyncMock(side_effect=aiohttp.ClientError("down")))
    for index, outcome in enumerate(outcomes):
        with patch(HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.77"})), patch(
            ESTABLISH, new=outcome
        ):
            ctx = await init_integration(hass, data={"host": f"192.168.99.{50 + index}"})
            await hass.async_block_till_done(wait_background_tasks=True)
        outcome.assert_awaited_once()
        assert CONF_TLS_SPKI not in ctx.entry.data


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
