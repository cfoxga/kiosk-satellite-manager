"""Use HTTPS: the per-device opt-in and the switch back to HTTP
(KSM-BEHAVE-169, #137), on a per-device entry and a native subentry."""
from __future__ import annotations

import asyncio
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import fleet, ks_tls
from custom_components.kiosk_satellite_manager import (
    acme_issuer, acme_renewal, config_flow, le_certificate,
)
from custom_components.kiosk_satellite_manager.const import (
    CONF_ACME_ACCOUNT_KEY, CONF_ACME_ACCOUNT_URL, CONF_ACME_DNS_TOKEN, CONF_ACME_EMAIL,
    CONF_ENTRY_TYPE, CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_TLS_SPKI, DOMAIN,
)
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

from .test_global_settings import _manager

PIN = "ab" * 32
HOST = "192.0.2.61"
_API = "custom_components.kiosk_satellite_manager.ks_api_client"


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch):
    monkeypatch.setattr(ks_tls, "TLS_ENABLE_POLL_DELAY_S", 0)


async def _open(hass, surface, *, pinned: bool, password: str | None = "secret",
                host: str = HOST, extra: dict | None = None):
    """Open Configure -> Use HTTPS. Returns (manager, flow_id, data reader, form)."""
    data = {CONF_HOST: host, CONF_NAME: "Display", CONF_PASSWORD: password, **(extra or {})}
    if pinned:
        data[CONF_TLS_SPKI] = PIN
    if surface == "entry":
        entry = MockConfigEntry(domain=DOMAIN, title="Display", unique_id=host, data=data)
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


NAME = "test-portal-mini.cfoxga.com"
NEW_PIN = "cd" * 32
TOKEN = "cf-token-never-shown"
DEVICE_KEY = "-----BEGIN PRIVATE KEY-----device-key-----END PRIVATE KEY-----"


def _material(key: str = DEVICE_KEY, spki: str = NEW_PIN):
    from datetime import datetime, timedelta, timezone
    return le_certificate.CertificateMaterial(
        "chain-pem", key, spki, "ef" * 32, datetime.now(timezone.utc) + timedelta(days=90))


def _certificates(hass):
    """KSM-BEHAVE-203 settings on the manager entry, adding one if needed."""
    manager = acme_renewal.manager_entry(hass)
    if manager is None:
        manager = MockConfigEntry(domain=DOMAIN, title="KSM Settings", unique_id="ksm_manager",
                                  data={CONF_ENTRY_TYPE: "manager"})
        manager.add_to_hass(hass)
    hass.config_entries.async_update_entry(manager, data={
        **manager.data, CONF_ACME_EMAIL: "ops@example.com", CONF_ACME_DNS_TOKEN: TOKEN,
        CONF_ACME_ACCOUNT_KEY: "account-key", CONF_ACME_ACCOUNT_URL: "https://acme.test/acct/1",
    })
    return manager


def _slow(outcome):
    """An issuance that yields before finishing, as a real one does."""
    async def run(*_args):
        await asyncio.sleep(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return AsyncMock(side_effect=run)


def _abort_spy():
    """Record abort reasons; a progress flow finishes inside HA's own
    follow-up configure, so the test never sees that result directly."""
    reasons: list[str] = []
    original = config_flow.KioskSatelliteManagerOptionsFlow.async_abort

    def spy(self, *, reason, **kwargs):
        reasons.append(reason)
        return original(self, reason=reason, **kwargs)

    return reasons, patch.object(config_flow.KioskSatelliteManagerOptionsFlow, "async_abort", spy)


async def _choose_acme(hass, manager, flow_id, *, pinned, hostname=NAME, issue=None, imported=None):
    """Submit the KSM-certificate choice and drive its progress step."""
    action = {"https_action": "acme"} if pinned else {"certificate_source": "acme"}
    with patch.object(acme_issuer, "async_issue", new=issue or _slow(_material())), patch.object(
        ks_tls, "async_import_certificate", new=imported or AsyncMock(return_value=NEW_PIN)
    ), patch(f"{_API}.probe_https_identity", new=AsyncMock(return_value=None)):
        result = await manager.async_configure(flow_id, {
            "confirm": True, **action, "certificate_hostname": hostname,
        })
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            assert result["progress_action"] == "acme_issue"
            await hass.async_block_till_done()
            result = await manager.async_configure(flow_id)
    return result


@pytest.mark.parametrize("surface", ["entry", "subentry"])
@pytest.mark.parametrize("pinned", [False, True])
async def test_ksm_certificate_is_issued_imported_and_stored(hass, surface, pinned, tls_migration):
    """[KSM-TEST-405] Unpinned devices are pinned first; then KSM issues,
    imports over the pin, and stores the certificate and the served pin."""
    tls_migration.return_value = PIN
    issue = _slow(_material())
    imported = AsyncMock(return_value=NEW_PIN)
    manager, flow_id, read, form = await _open(hass, surface, pinned=pinned)
    _certificates(hass)
    schema = str(form["data_schema"])
    assert "certificate_hostname" in schema
    reasons, spy = _abort_spy()
    with spy:
        await _choose_acme(hass, manager, flow_id, pinned=pinned, issue=issue, imported=imported)
    assert reasons == ["https_enabled"]
    assert tls_migration.await_count == (0 if pinned else 1)
    issue.assert_awaited_once()
    assert issue.await_args.args[2:] == (NAME, None)
    assert issue.await_args.args[1][CONF_ACME_DNS_TOKEN] == TOKEN
    assert imported.await_args.args[1:4] == (HOST, "secret", PIN)
    data = read()
    assert data[CONF_TLS_SPKI] == NEW_PIN
    assert data["acme_hostname"] == NAME
    assert data["acme_certificate"] == "chain-pem"
    assert data["acme_private_key"] == DEVICE_KEY
    assert data["acme_fingerprint"] == "ef" * 32
    assert data["acme_expires"]
    assert "le_certificate_hostname" not in data


@pytest.mark.parametrize("host,expected", [("portal.cfoxga.com", "portal.cfoxga.com"), (HOST, "")])
async def test_certificate_hostname_prefills_only_from_a_dns_host(hass, host, expected):
    """[KSM-TEST-405] The name field holds a DNS host, and is empty for an IP."""
    _manager_, _flow, _read, form = await _open(hass, "entry", pinned=False, host=host)
    field = next(k for k in form["data_schema"].schema if str(k) == "certificate_hostname")
    assert field.default() == expected


@pytest.mark.parametrize("pinned", [False, True])
async def test_no_certificates_settings_aborts_before_any_device_call(hass, pinned, tls_migration):
    """[KSM-TEST-405] Negative: no Certificates settings -> acme_not_configured, no device call."""
    issue, imported = AsyncMock(), AsyncMock()
    manager, flow_id, read, _ = await _open(hass, "entry", pinned=pinned)
    before = read()
    result = await _choose_acme(hass, manager, flow_id, pinned=pinned, issue=issue, imported=imported)
    assert result["reason"] == "acme_not_configured"
    tls_migration.assert_not_awaited()
    issue.assert_not_awaited()
    imported.assert_not_awaited()
    assert read() == before


@pytest.mark.parametrize("pinned", [False, True])
async def test_issuance_failure_imports_nothing(hass, pinned, tls_migration):
    """[KSM-TEST-405] Negative: a failed issuance aborts https_certificate_unavailable
    with its fixed message; the device keeps the pin it had (or step 1's)."""
    tls_migration.return_value = PIN
    _certificates(hass)
    imported = AsyncMock()
    reasons, spy = _abort_spy()
    manager, flow_id, read, _ = await _open(hass, "entry", pinned=pinned)
    with spy:
        await _choose_acme(hass, manager, flow_id, pinned=pinned, imported=imported, issue=_slow(
            le_certificate.CertificateUnavailable("Let's Encrypt did not issue the certificate")))
    assert reasons == ["https_certificate_unavailable"]
    imported.assert_not_awaited()
    assert read()[CONF_TLS_SPKI] == PIN
    assert "acme_hostname" not in read()


async def test_import_failure_keeps_the_established_pin(hass, tls_migration):
    """[KSM-TEST-405] Negative: the device rejects the import -> the pin
    step 1 established stays, and no certificate is stored."""
    tls_migration.return_value = PIN
    _certificates(hass)
    reasons, spy = _abort_spy()
    manager, flow_id, read, _ = await _open(hass, "entry", pinned=False)
    with spy:
        await _choose_acme(hass, manager, flow_id, pinned=False,
                           imported=AsyncMock(side_effect=KsApiError("import rejected")))
    assert reasons == ["https_failed"]
    assert read()[CONF_TLS_SPKI] == PIN
    assert "acme_hostname" not in read()


async def test_certificate_finishing_after_the_dialog_closed_is_never_imported(hass, tls_migration):
    """[KSM-TEST-405] Negative: closing the dialog does not cancel issuance,
    and its certificate is discarded rather than installed."""
    tls_migration.return_value = PIN
    _certificates(hass)
    release, finished = asyncio.Event(), []

    async def run(*_args):
        await release.wait()
        finished.append(True)
        return _material()

    imported = AsyncMock(return_value=NEW_PIN)
    manager, flow_id, read, _ = await _open(hass, "entry", pinned=True)
    with patch.object(acme_issuer, "async_issue", new=AsyncMock(side_effect=run)), patch.object(
        ks_tls, "async_import_certificate", new=imported
    ):
        result = await manager.async_configure(flow_id, {
            "confirm": True, "https_action": "acme", "certificate_hostname": NAME,
        })
        assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS, result
        manager.async_abort(flow_id)
        await asyncio.sleep(0)
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert finished == [True]
    imported.assert_not_awaited()
    assert read()[CONF_TLS_SPKI] == PIN
    assert "acme_hostname" not in read()


async def test_hostname_of_another_device_aborts_before_issuance(hass, tls_migration):
    """[KSM-TEST-410] Negative: a name another managed device holds aborts
    hostname_in_use, with no issuance and no device call."""
    _certificates(hass)
    MockConfigEntry(domain=DOMAIN, title="Other", unique_id="192.0.2.99", data={
        CONF_HOST: "192.0.2.99", CONF_NAME: "Other", CONF_TLS_SPKI: "99" * 32,
        "acme_hostname": NAME,
    }).add_to_hass(hass)
    issue = AsyncMock()
    manager, flow_id, read, _ = await _open(hass, "entry", pinned=False)
    result = await _choose_acme(hass, manager, flow_id, pinned=False, issue=issue)
    assert result["reason"] == "hostname_in_use"
    issue.assert_not_awaited()
    tls_migration.assert_not_awaited()
    assert CONF_TLS_SPKI not in read()


@pytest.mark.parametrize("hostname", ["192.0.2.10", "portal.local"])
async def test_ip_or_local_name_aborts_before_issuance(hass, tls_migration, hostname):
    """[KSM-TEST-405] Negative: a name Let's Encrypt cannot issue for aborts
    `https_certificate_unavailable` with no issuance and no device call."""
    _certificates(hass)
    issue = AsyncMock()
    manager, flow_id, read, _ = await _open(hass, "entry", pinned=False)
    result = await _choose_acme(hass, manager, flow_id, pinned=False, hostname=hostname, issue=issue)
    assert result["reason"] == "https_certificate_unavailable"
    issue.assert_not_awaited()
    tls_migration.assert_not_awaited()
    assert CONF_TLS_SPKI not in read()


async def test_same_name_again_reuses_the_device_key(hass, tls_migration):
    """[KSM-TEST-410] A pinned device re-choosing its own name keeps its own
    key (stable pin); it never takes another device's."""
    _certificates(hass)
    issue = _slow(_material())
    manager, flow_id, read, _ = await _open(hass, "entry", pinned=True, extra={
        "acme_hostname": NAME, "acme_private_key": DEVICE_KEY,
    })
    await _choose_acme(hass, manager, flow_id, pinned=True, issue=issue)
    assert issue.await_args.args[2:] == (NAME, DEVICE_KEY)


_CERT_FIELDS = {
    "acme_hostname": NAME, "acme_certificate": "chain-pem", "acme_private_key": DEVICE_KEY,
    "acme_fingerprint": "ef" * 32, "acme_expires": "2027-01-01T00:00:00+00:00",
}


@pytest.mark.parametrize("surface", ["entry", "subentry"])
async def test_switch_back_to_http_deletes_the_certificate(hass, surface):
    """[KSM-TEST-409] Switching back to HTTP deletes the certificate fields
    and the renewal repair."""
    manager, flow_id, read, _ = await _open(hass, surface, pinned=True, extra=_CERT_FIELDS)
    device_id = "https-sub" if surface == "subentry" else next(
        e.entry_id for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == HOST)
    ir.async_create_issue(hass, DOMAIN, f"acme_renewal_failed_{device_id}", is_fixable=True,
                          severity=ir.IssueSeverity.ERROR, translation_key="acme_renewal_failed",
                          translation_placeholders={"name": "Display", "hostname": NAME},
                          data={"entry_id": device_id})
    with patch(f"{_API}.login", new=AsyncMock(return_value="tok")), patch(
        f"{_API}.patch_settings", new=AsyncMock(return_value={"ok": True})
    ), patch(f"{_API}.get_health", new=AsyncMock(return_value={"appVersion": "2026.9.90"})):
        result = await manager.async_configure(flow_id, {"confirm": True})
    assert result["reason"] == "https_disabled"
    assert not set(_CERT_FIELDS) & set(read())
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"acme_renewal_failed_{device_id}") is None


async def test_failed_switch_back_keeps_the_certificate(hass):
    """[KSM-TEST-409] Negative: a refused switch back keeps every field."""
    manager, flow_id, read, _ = await _open(hass, "entry", pinned=True, extra=_CERT_FIELDS)
    with patch(f"{_API}.login", new=AsyncMock(side_effect=KsApiError("invalid password"))):
        result = await manager.async_configure(flow_id, {"confirm": True})
    assert result["reason"] == "https_failed"
    assert set(_CERT_FIELDS) <= set(read())
