"""[KSM-TEST-402/403] Certificates settings on the manager entry (KSM-BEHAVE-203, #200)."""
from __future__ import annotations

import dataclasses
import json
import logging
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant import data_entry_flow
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import (
    acme_issuer, acme_renewal, diagnostics, dns_cloudflare,
)
from custom_components.kiosk_satellite_manager.const import (
    CONF_ACME_ACCOUNT_KEY, CONF_ACME_ACCOUNT_URL, CONF_ACME_DNS_PROVIDER, CONF_ACME_DNS_TOKEN,
    CONF_ACME_EMAIL, DOMAIN,
)
from custom_components.kiosk_satellite_manager.websocket_api import build_tree

from .test_global_settings import _manager

TOKEN = "cf-token-never-shown"
NEW_TOKEN = "cf-token-second"
ZONES = [dns_cloudflare.Zone("z1", "cfoxga.com", ("a.ns.test",))]
CF = "https://api.cloudflare.com/client/v4"
GOOD = {CONF_ACME_EMAIL: "ops@example.com", CONF_ACME_DNS_PROVIDER: "cloudflare",
        CONF_ACME_DNS_TOKEN: TOKEN}


async def _certificates_step(hass, manager):
    flow = await hass.config_entries.options.async_init(manager.entry_id)
    assert flow["type"] == data_entry_flow.FlowResultType.MENU
    assert flow["menu_options"] == ["settings", "certificates"]
    form = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "certificates"})
    assert form["step_id"] == "certificates"
    return form


def _cloudflare(*, verify=None, zones=ZONES):
    return (
        patch.object(dns_cloudflare, "async_verify_token", new=AsyncMock(side_effect=verify)),
        patch.object(dns_cloudflare, "async_zones", new=AsyncMock(return_value=zones)),
    )


async def _submit(hass, manager, user_input, *, verify=None, zones=ZONES, register=None):
    form = await _certificates_step(hass, manager)
    verify_patch, zones_patch = _cloudflare(verify=verify, zones=zones)
    register = register or AsyncMock(return_value=("ACCOUNT-KEY-PEM", "https://acme.test/acct/1"))
    with verify_patch, zones_patch, patch.object(acme_issuer, "async_register", new=register):
        result = await hass.config_entries.options.async_configure(form["flow_id"], user_input)
        await hass.async_block_till_done(wait_background_tasks=True)
    return result, register


async def test_valid_settings_store_credentials_and_account(hass):
    """[KSM-TEST-402] Email, provider, token, account key and URL are stored."""
    manager = await _manager(hass)
    form = await _certificates_step(hass, manager)
    assert "acme_dns_token" in str(form["data_schema"].schema)
    result, register = await _submit(hass, manager, GOOD)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    register.assert_awaited_once()
    assert register.await_args.args[1] == "ops@example.com"
    data = manager.data
    assert data[CONF_ACME_EMAIL] == "ops@example.com"
    assert data[CONF_ACME_DNS_PROVIDER] == "cloudflare"
    assert data[CONF_ACME_DNS_TOKEN] == TOKEN
    assert data[CONF_ACME_ACCOUNT_KEY] == "ACCOUNT-KEY-PEM"
    assert data[CONF_ACME_ACCOUNT_URL] == "https://acme.test/acct/1"
    assert acme_renewal.acme_settings(hass) is not None


@pytest.mark.parametrize("change,verify,zones,error", [
    ({CONF_ACME_EMAIL: "not-an-email"}, None, ZONES, {CONF_ACME_EMAIL: "invalid_email"}),
    ({}, dns_cloudflare.CloudflareError("invalid_token"), ZONES, {CONF_ACME_DNS_TOKEN: "invalid_token"}),
    ({}, None, [], {CONF_ACME_DNS_TOKEN: "no_zones"}),
    ({}, dns_cloudflare.CloudflareError("cannot_connect"), ZONES, {"base": "cannot_connect"}),
])
async def test_each_failure_shows_its_error_and_stores_nothing(hass, change, verify, zones, error):
    """[KSM-TEST-402] Negative: bad email, inactive token, no zones, network error."""
    manager = await _manager(hass)
    before = dict(manager.data)
    result, register = await _submit(hass, manager, {**GOOD, **change}, verify=verify, zones=zones)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == error
    register.assert_not_awaited()
    assert dict(manager.data) == before


async def test_registration_failure_stores_nothing(hass):
    """[KSM-TEST-402] Negative: Let's Encrypt refuses the account."""
    manager = await _manager(hass)
    before = dict(manager.data)
    from custom_components.kiosk_satellite_manager.le_certificate import CertificateUnavailable
    result, _ = await _submit(hass, manager, GOOD, register=AsyncMock(
        side_effect=CertificateUnavailable("Let's Encrypt account registration failed")))
    assert result["errors"] == {"base": "acme_registration_failed"}
    assert dict(manager.data) == before


async def test_new_token_keeps_account_and_remove_deletes_only_the_token(hass):
    """[KSM-TEST-402] Saving a new token keeps the account key; a blank token
    keeps the saved one; Remove deletes the token only."""
    manager = await _manager(hass)
    await _submit(hass, manager, GOOD)
    result, register = await _submit(hass, manager, {**GOOD, CONF_ACME_DNS_TOKEN: NEW_TOKEN})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    register.assert_not_awaited()
    assert manager.data[CONF_ACME_DNS_TOKEN] == NEW_TOKEN
    assert manager.data[CONF_ACME_ACCOUNT_KEY] == "ACCOUNT-KEY-PEM"
    blank = {k: v for k, v in GOOD.items() if k != CONF_ACME_DNS_TOKEN}
    await _submit(hass, manager, blank)
    assert manager.data[CONF_ACME_DNS_TOKEN] == NEW_TOKEN
    result, _ = await _submit(hass, manager, {**blank, "acme_remove": True})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert CONF_ACME_DNS_TOKEN not in manager.data
    assert manager.data[CONF_ACME_ACCOUNT_KEY] == "ACCOUNT-KEY-PEM"
    assert acme_renewal.acme_settings(hass) is None


async def test_removing_settings_deletes_no_device_field(hass):
    """[KSM-TEST-409] Negative: Remove leaves every device's certificate alone."""
    manager = await _manager(hass)
    await _submit(hass, manager, GOOD)
    device = MockConfigEntry(domain=DOMAIN, title="Mini", unique_id="mini", data={
        "host": "192.0.2.61", "tls_spki_sha256": "aa" * 32, "acme_hostname": "mini.cfoxga.com",
        "acme_private_key": "DEVICE-KEY", "acme_certificate": "chain", "acme_expires": "2027-01-01",
    })
    device.add_to_hass(hass)
    before = dict(device.data)
    await _submit(hass, manager, {**GOOD, "acme_remove": True})
    assert dict(device.data) == before


async def test_secrets_never_reach_logs_errors_snapshot_or_diagnostics(
    hass, aioclient_mock, caplog
):
    """[KSM-TEST-403] Real Cloudflare client code: a transport error whose text
    holds the token shows a fixed error; the token, account key and device
    key appear in no log, form, repair, panel snapshot or diagnostics."""
    caplog.set_level(logging.DEBUG)
    manager = await _manager(hass)
    aioclient_mock.get(f"{CF}/user/tokens/verify", json={"success": True, "result": {"status": "active"}})
    aioclient_mock.get(f"{CF}/zones", exc=aiohttp.ClientError(f"proxy said {TOKEN}"))
    form = await _certificates_step(hass, manager)
    result = await hass.config_entries.options.async_configure(form["flow_id"], GOOD)
    assert result["errors"] == {"base": "cannot_connect"}
    assert TOKEN not in json.dumps(result, default=str)

    hass.config_entries.async_update_entry(manager, data={
        **manager.data, CONF_ACME_DNS_TOKEN: TOKEN, CONF_ACME_ACCOUNT_KEY: "ACCOUNT-KEY-PEM",
        CONF_ACME_ACCOUNT_URL: "https://acme.test/acct/1", CONF_ACME_EMAIL: "ops@example.com",
    })
    device = MockConfigEntry(domain=DOMAIN, title="Mini", unique_id="mini", data={
        "host": "192.0.2.61", "tls_spki_sha256": "aa" * 32, "acme_hostname": "mini.cfoxga.com",
        "acme_private_key": "DEVICE-KEY-PEM", "acme_certificate": "chain",
        "acme_expires": "2020-01-01T00:00:00+00:00",
    })
    device.add_to_hass(hass)
    with patch.object(acme_issuer, "_protocol", side_effect=RuntimeError(
            f"{TOKEN} ACCOUNT-KEY-PEM DEVICE-KEY-PEM")), patch.object(
            dns_cloudflare, "async_zones", new=AsyncMock(return_value=ZONES)):
        assert await acme_renewal.async_check_device(hass, device) is False
    issues = [i for (d, _), i in ir.async_get(hass).issues.items() if d == DOMAIN]
    assert any(i.issue_id == f"acme_renewal_failed_{device.entry_id}" for i in issues)
    tree = json.dumps(build_tree(hass), default=str)
    diag = json.dumps([
        await diagnostics.async_get_config_entry_diagnostics(hass, manager),
        await diagnostics.async_get_config_entry_diagnostics(hass, device),
    ], default=str)
    issue_text = json.dumps([dataclasses.asdict(i) for i in issues], default=str)
    for secret in (TOKEN, "ACCOUNT-KEY-PEM", "DEVICE-KEY-PEM"):
        assert secret not in caplog.text, secret
        assert secret not in tree, secret
        assert secret not in diag, secret
        assert secret not in issue_text, secret


async def test_setup_repair_is_one_issue_and_its_fix_saves_settings(hass):
    """[KSM-TEST-408] Without settings, one acme_setup_required repair for any
    number of legacy devices; its fix flow is the Certificates step."""
    manager = await _manager(hass)
    for n in (1, 2):
        MockConfigEntry(domain=DOMAIN, title=f"P{n}", unique_id=f"p{n}", data={
            "host": f"192.0.2.{n}", "tls_spki_sha256": "aa" * 32,
            "le_certificate_hostname": f"p{n}.cfoxga.com",
        }).add_to_hass(hass)
    acme_renewal.sync_setup_repair(hass)
    ours = [k for k in ir.async_get(hass).issues if k[0] == DOMAIN and k[1].startswith("acme_setup")]
    assert ours == [(DOMAIN, "acme_setup_required")]

    from custom_components.kiosk_satellite_manager import repairs
    flow = await repairs.async_create_fix_flow(hass, "acme_setup_required", None)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["step_id"] == "certificates"
    verify_patch, zones_patch = _cloudflare()
    with verify_patch, zones_patch, patch.object(acme_issuer, "async_register", new=AsyncMock(
            return_value=("ACCOUNT-KEY-PEM", "https://acme.test/acct/1"))), patch.object(
            acme_renewal, "async_tick", new=AsyncMock()):
        done = await flow.async_step_certificates(GOOD)
    assert done["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert manager.data[CONF_ACME_DNS_TOKEN] == TOKEN
    assert ir.async_get(hass).async_get_issue(DOMAIN, "acme_setup_required") is None
