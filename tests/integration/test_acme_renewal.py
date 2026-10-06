"""[KSM-TEST-407/408/410] Renewal and migration of KSM-issued device
certificates (KSM-BEHAVE-206/207/209, #200)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.kiosk_satellite_manager as integration
from custom_components.kiosk_satellite_manager import (
    acme_issuer, acme_renewal, ks_tls, le_certificate, le_certificate_sync,
)
from custom_components.kiosk_satellite_manager.const import (
    CONF_ACME_ACCOUNT_KEY, CONF_ACME_ACCOUNT_URL, CONF_ACME_DNS_TOKEN, CONF_ACME_EMAIL,
    CONF_ENTRY_TYPE, CONF_HOST, CONF_PASSWORD, CONF_TLS_SPKI, DOMAIN,
)

from .test_global_settings import _manager

PIN = "aa" * 32
NAME = "mini.cfoxga.com"
KEY = "-----BEGIN PRIVATE KEY-----stored-----END PRIVATE KEY-----"
_API = "custom_components.kiosk_satellite_manager.ks_api_client"


def _settings(hass):
    manager = acme_renewal.manager_entry(hass)
    if manager is None:
        manager = MockConfigEntry(domain=DOMAIN, title="KSM Settings", unique_id="ksm_manager",
                                  data={CONF_ENTRY_TYPE: "manager"})
        manager.add_to_hass(hass)
    hass.config_entries.async_update_entry(manager, data={
        **manager.data, CONF_ACME_EMAIL: "ops@example.com", CONF_ACME_DNS_TOKEN: "tok",
        CONF_ACME_ACCOUNT_KEY: "acct", CONF_ACME_ACCOUNT_URL: "https://acme.test/acct/1",
    })
    return manager


def _device(hass, *, days_left: float | None = None, legacy: bool = False, n: int = 1):
    data = {CONF_HOST: f"192.0.2.{60 + n}", CONF_PASSWORD: "secret", CONF_TLS_SPKI: PIN}
    if days_left is not None:
        data.update({
            "acme_hostname": NAME, "acme_certificate": "old-chain", "acme_private_key": KEY,
            "acme_fingerprint": "old-fp",
            "acme_expires": (datetime.now(timezone.utc) + timedelta(days=days_left)).isoformat(),
        })
    if legacy:
        data.update({"le_certificate_hostname": f"p{n}.cfoxga.com",
                     "le_certificate_fingerprint": "shared-fp"})
    entry = MockConfigEntry(domain=DOMAIN, title=f"Portal {n}", unique_id=f"portal-{n}", data=data)
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = MagicMock(async_request_refresh=AsyncMock())
    return entry


def _material(key=KEY, spki=PIN, fp="new-fp"):
    return le_certificate.CertificateMaterial(
        "new-chain", key, spki, fp, datetime.now(timezone.utc) + timedelta(days=90))


def _io(*, issue=None, served=None, imported=None):
    issue = issue or AsyncMock(return_value=_material())
    imported = imported or AsyncMock(return_value=PIN)
    probe = AsyncMock(return_value=served)
    return issue, imported, probe, (
        patch.object(acme_issuer, "async_issue", new=issue),
        patch.object(ks_tls, "async_import_certificate", new=imported),
        patch(f"{_API}.probe_https_identity", new=probe),
    )


async def _run(hass, patches, coro_fn):
    with patches[0], patches[1], patches[2]:
        return await coro_fn()


async def test_renewal_inside_30_days_reuses_the_stored_key(hass):
    """[KSM-TEST-407] 29 days left: re-issue with the stored key, import over
    the existing pin, which is unchanged."""
    _settings(hass)
    device = _device(hass, days_left=29)
    issue, imported, _probe, patches = _io()
    assert await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, device))
    assert issue.await_args.args[2:] == (NAME, KEY)
    assert imported.await_args.args[1:4] == ("192.0.2.61", "secret", PIN)
    assert device.data[CONF_TLS_SPKI] == PIN
    assert device.data["acme_certificate"] == "new-chain"
    assert device.data["acme_fingerprint"] == "new-fp"
    assert datetime.fromisoformat(device.data["acme_expires"]) > datetime.now(timezone.utc) + timedelta(days=80)


async def test_device_already_serving_the_new_certificate_is_adopted(hass):
    """[KSM-TEST-407] The device serves the new fingerprint: store, no import."""
    _settings(hass)
    device = _device(hass, days_left=10)
    issue, imported, probe, patches = _io(served=(PIN, "new-fp"))
    assert await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, device))
    imported.assert_not_awaited()
    assert device.data["acme_fingerprint"] == "new-fp"


async def test_31_days_left_does_nothing(hass):
    """[KSM-TEST-407] Negative: outside the renewal window nothing runs."""
    _settings(hass)
    device = _device(hass, days_left=31)
    issue, imported, probe, patches = _io()
    assert not await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, device))
    issue.assert_not_awaited()
    probe.assert_not_awaited()


async def test_failure_repair_only_under_21_days_and_success_clears_it(hass):
    """[KSM-TEST-407] Negative: a failure keeps everything; at 25 days no repair,
    at 20 days `acme_renewal_failed_<id>`; a later success deletes it."""
    _settings(hass)
    failing = AsyncMock(side_effect=le_certificate.CertificateUnavailable("Cloudflare could not be reached"))
    for days, repaired in ((25, False), (20, True)):
        device = _device(hass, days_left=days, n=days)
        before = dict(device.data)
        _issue, imported, _probe, patches = _io(issue=failing)
        assert not await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, device))
        imported.assert_not_awaited()
        assert dict(device.data) == before
        issue_id = f"acme_renewal_failed_{device.entry_id}"
        assert (ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None) is repaired
    _issue, _imported, _probe, patches = _io()
    assert await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, device))
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_no_settings_counts_as_a_failed_renewal(hass):
    """[KSM-TEST-407] Negative: without Certificates settings a due renewal fails."""
    device = _device(hass, days_left=5)
    issue, _imported, _probe, patches = _io()
    assert not await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, device))
    issue.assert_not_awaited()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"acme_renewal_failed_{device.entry_id}")


async def test_repair_fix_flow_renews_now(hass):
    """[KSM-TEST-407] The repair's fix flow runs one renewal attempt."""
    from custom_components.kiosk_satellite_manager import repairs

    _settings(hass)
    device = _device(hass, days_left=40)
    issue_id = f"acme_renewal_failed_{device.entry_id}"
    flow = await repairs.async_create_fix_flow(hass, issue_id, {"entry_id": device.entry_id})
    assert isinstance(flow, repairs.AcmeRenewalFailedFlow)
    flow.hass = hass
    assert (await flow.async_step_init())["step_id"] == "confirm"
    issue, _imported, _probe, patches = _io()

    async def renew():
        progress = await flow.async_step_confirm({})
        assert progress["type"] == "progress" and progress["progress_action"] == "acme_renew"
        await flow._task
        done = await flow.async_step_renew()
        assert done["type"] == "progress_done" and done["step_id"] == "renewed"
        return await flow.async_step_renewed()
    result = await _run(hass, patches, renew)
    assert result["type"] == "create_entry"
    issue.assert_awaited_once()


async def test_manager_unload_cancels_the_renewal_timer(hass):
    """[KSM-TEST-407] The six-hour tick belongs to the manager entry."""
    callbacks, cancel = [], MagicMock()
    original = integration.async_track_time_interval

    def track(hass_arg, callback, interval, *, name):
        if name.endswith("certificate_renewal"):
            callbacks.append((callback, interval))
            return cancel
        return original(hass_arg, callback, interval, name=name)

    with patch.object(integration, "async_track_time_interval", new=track):
        manager = await _manager(hass)
    assert [interval for _cb, interval in callbacks] == [timedelta(hours=6)]
    with patch.object(acme_renewal, "async_device_tick", new=AsyncMock()) as tick:
        _device(hass, days_left=5)
        await callbacks[0][0](datetime.now(timezone.utc))
    tick.assert_awaited_once()
    await hass.config_entries.async_unload(manager.entry_id)
    cancel.assert_called_once()


async def test_legacy_device_migrates_to_a_new_key(hass):
    """[KSM-TEST-408/410] With settings, a legacy device gets a new key and
    certificate for its stored name, and the `le_certificate_*` fields go;
    the shared `/ssl` key is never read."""
    _settings(hass)
    device = _device(hass, legacy=True)
    issue, imported, _probe, patches = _io(issue=AsyncMock(return_value=_material(
        key="NEW-KEY", spki="bb" * 32)), imported=AsyncMock(return_value="bb" * 32))
    with patch.object(le_certificate, "load_for_hostname") as ssl_read, patch.object(
            le_certificate_sync, "async_sync_device", new=AsyncMock()) as sync:
        await _run(hass, patches, lambda: acme_renewal.async_tick(hass))
    assert issue.await_args.args[2:] == ("p1.cfoxga.com", None)
    assert imported.await_args.args[3] == PIN
    ssl_read.assert_not_called()
    sync.assert_not_awaited()
    data = device.data
    assert data["acme_hostname"] == "p1.cfoxga.com"
    assert data["acme_private_key"] == "NEW-KEY"
    assert data[CONF_TLS_SPKI] == "bb" * 32
    assert "le_certificate_hostname" not in data and "le_certificate_fingerprint" not in data


async def test_failed_migration_keeps_the_shared_certificate(hass):
    """[KSM-TEST-408] Negative: issuance fails -> legacy fields and pin stay."""
    _settings(hass)
    device = _device(hass, legacy=True)
    before = dict(device.data)
    _issue, imported, _probe, patches = _io(issue=AsyncMock(
        side_effect=le_certificate.CertificateUnavailable("No Cloudflare zone holds this hostname")))
    with patch.object(le_certificate_sync, "async_sync_device", new=AsyncMock()) as sync:
        await _run(hass, patches, lambda: acme_renewal.async_tick(hass))
    imported.assert_not_awaited()
    assert dict(device.data) == before
    # Until it migrates, the shared certificate keeps renewing.
    sync.assert_awaited_once()


async def test_without_settings_legacy_devices_sync_and_migrated_never_do(hass):
    """[KSM-TEST-408] Negative: no settings -> one setup repair, `/ssl` sync for
    legacy devices only; a KSM-certificate device never runs it."""
    legacy = [_device(hass, legacy=True, n=n) for n in (1, 2)]
    migrated = _device(hass, days_left=60, n=3)
    with patch.object(le_certificate_sync, "async_sync_device", new=AsyncMock()) as sync:
        await acme_renewal.async_tick(hass)
    assert sorted(c.args[1].entry_id for c in sync.await_args_list) == sorted(
        d.entry_id for d in legacy)
    assert migrated.entry_id not in [c.args[1].entry_id for c in sync.await_args_list]
    setup = [k for k in ir.async_get(hass).issues if k == (DOMAIN, "acme_setup_required")]
    assert len(setup) == 1
    _settings(hass)
    acme_renewal.sync_setup_repair(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "acme_setup_required") is None


def test_no_code_reaches_the_letsencrypt_addon():
    """[KSM-TEST-408] `le_addon` is gone and nothing calls the Supervisor add-on API."""
    root = Path(integration.__file__).parent
    assert not (root / "le_addon.py").exists()
    for source in root.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "core_letsencrypt" not in text, source.name
        assert "hassio" not in text, source.name


async def test_devices_with_nothing_to_do_are_left_alone(hass):
    """[KSM-TEST-407/408] Negative: a KSM certificate without a pin, a legacy
    device without settings or a pin, and a device with no certificate never
    issue; an unreadable expiry counts as due."""
    issue, imported, _probe, patches = _io()
    unpinned = _device(hass, days_left=5, n=1)
    hass.config_entries.async_update_entry(unpinned, data={
        k: v for k, v in unpinned.data.items() if k != CONF_TLS_SPKI})
    legacy = _device(hass, legacy=True, n=2)
    plain = _device(hass, n=3)
    with patch.object(le_certificate_sync, "async_sync_device", new=AsyncMock()) as sync:
        for device in (unpinned, legacy):
            assert not await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, device))
        await acme_renewal.async_device_tick(hass, plain)
    issue.assert_not_awaited()
    sync.assert_not_awaited()

    _settings(hass)
    garbled = _device(hass, days_left=60, n=4)
    hass.config_entries.async_update_entry(garbled, data={**garbled.data, "acme_expires": "soon"})
    issue, _imported, _probe, patches = _io()
    assert await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, garbled))
    issue.assert_awaited_once()


async def test_repair_fix_flows_fail_safely(hass):
    """[KSM-TEST-407/408] Negative: a failed retry aborts `renewal_failed`; a
    removed device or manager aborts `entry_not_found`; a bad setup save
    re-shows the form with its error."""
    from custom_components.kiosk_satellite_manager import repairs

    _settings(hass)
    device = _device(hass, days_left=5)
    flow = await repairs.async_create_fix_flow(
        hass, f"acme_renewal_failed_{device.entry_id}", {"entry_id": device.entry_id})
    flow.hass = hass
    _issue, _imported, _probe, patches = _io(issue=AsyncMock(
        side_effect=le_certificate.CertificateUnavailable("Cloudflare could not be reached")))

    async def renew():
        await flow.async_step_confirm({})
        await flow._task
        done = await flow.async_step_renew()
        assert done["step_id"] == "renew_failed"
        return await flow.async_step_renew_failed()
    assert (await _run(hass, patches, renew))["reason"] == "renewal_failed"
    gone = await repairs.async_create_fix_flow(
        hass, "acme_renewal_failed_missing", {"entry_id": "missing"})
    gone.hass = hass
    assert (await gone.async_step_init())["reason"] == "entry_not_found"

    setup = await repairs.async_create_fix_flow(hass, "acme_setup_required", None)
    setup.hass = hass
    form = await setup.async_step_certificates({CONF_ACME_EMAIL: "nope", "acme_dns_provider": "cloudflare"})
    assert form["step_id"] == "certificates" and form["errors"] == {CONF_ACME_EMAIL: "invalid_email"}
    await hass.config_entries.async_remove(acme_renewal.manager_entry(hass).entry_id)
    assert (await setup.async_step_init())["reason"] == "entry_not_found"


def _real_material(hostname):
    """A real self-signed certificate `validate(exact=True)` accepts."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return le_certificate.validate(cert.public_bytes(serialization.Encoding.PEM), key_pem,
                                   hostname, exact=True)


async def test_failed_import_keeps_the_new_key_and_the_next_attempt_adopts_it(hass):
    """[KSM-TEST-408/410] Negative: the import of a migration's new key fails
    after the device took it. The key and certificate are kept as pending, the
    next attempt orders nothing and adopts the device serving them; the
    pending fields then go."""
    _settings(hass)
    device = _device(hass, legacy=True)
    material = _real_material("p1.cfoxga.com")
    issue, _imported, _probe, patches = _io(
        issue=AsyncMock(return_value=material),
        imported=AsyncMock(side_effect=ks_tls.KsApiError("new identity was not served")))
    assert not await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, device))
    pending = device.data["acme_pending"]
    assert pending["private_key"] == material.private_key
    assert device.data[CONF_TLS_SPKI] == PIN and "le_certificate_hostname" in device.data

    issue, imported, _probe, patches = _io(served=(material.spki_sha256, material.fingerprint))
    assert await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, device))
    issue.assert_not_awaited()
    imported.assert_not_awaited()
    assert device.data[CONF_TLS_SPKI] == material.spki_sha256
    assert device.data["acme_private_key"] == material.private_key
    assert "acme_pending" not in device.data and "le_certificate_hostname" not in device.data


async def test_pending_certificate_is_imported_without_a_new_order(hass):
    """[KSM-TEST-408] A pending certificate the device is not serving yet is
    imported as-is; one for another name, or garbage, is ignored."""
    _settings(hass)
    device = _device(hass, legacy=True)
    material = _real_material("p1.cfoxga.com")
    pending = {"hostname": "p1.cfoxga.com", "certificate": material.certificate,
               "private_key": material.private_key}
    for bad in ({**pending, "hostname": "other.cfoxga.com"}, {**pending, "certificate": "junk"}):
        hass.config_entries.async_update_entry(device, data={**device.data, "acme_pending": bad})
        assert acme_renewal._pending(device, "p1.cfoxga.com") is None
    hass.config_entries.async_update_entry(device, data={**device.data, "acme_pending": pending})
    issue, imported, _probe, patches = _io(imported=AsyncMock(return_value=material.spki_sha256))
    assert await _run(hass, patches, lambda: acme_renewal.async_check_device(hass, device))
    issue.assert_not_awaited()
    assert imported.await_args.args[4].certificate == material.certificate


async def test_one_failing_device_does_not_stop_the_tick(hass):
    """Negative: an unexpected error on one device still checks the next."""
    first, second = _device(hass, days_left=5, n=1), _device(hass, days_left=5, n=2)
    seen = []

    async def tick(_hass, device):
        seen.append(device.entry_id)
        if device is first or device.entry_id == first.entry_id:
            raise RuntimeError("boom")
    with patch.object(acme_renewal, "async_device_tick", new=tick):
        await acme_renewal.async_tick(hass)
    assert seen == [first.entry_id, second.entry_id]


async def test_pending_certificate_near_expiry_is_ignored(hass):
    """[KSM-TEST-407] Negative: a pending certificate with under 30 days left is not reused."""
    device = _device(hass, legacy=True)
    material = _real_material("p1.cfoxga.com")
    pending = {"hostname": "p1.cfoxga.com", "certificate": material.certificate,
               "private_key": material.private_key}
    hass.config_entries.async_update_entry(device, data={**device.data, "acme_pending": pending})
    assert acme_renewal._pending(device, "p1.cfoxga.com") is not None
    with patch.object(acme_renewal, "RENEW_BEFORE", timedelta(days=120)):
        assert acme_renewal._pending(device, "p1.cfoxga.com") is None


async def test_repair_renewal_that_crashes_or_is_cancelled_is_a_failure(hass):
    """[KSM-TEST-407] Negative: the fix flow's background attempt raising or
    being cancelled reports renew_failed, never renewed."""
    from custom_components.kiosk_satellite_manager import repairs

    async def boom():
        raise RuntimeError("boom")

    async def hang():
        await asyncio.sleep(3600)
    crashed = hass.async_create_task(boom())
    assert await repairs._finished(crashed) is False
    cancelled = hass.async_create_task(hang())
    cancelled.cancel()
    assert await repairs._finished(cancelled) is False
