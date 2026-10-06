"""[KSM-TEST-404] KSM's own ACME DNS-01 issuer (KSM-BEHAVE-204, #200).

The ACME protocol adapter is a fake that signs the CSR with a test CA, so the
issuer's own steps run for real: hostname checks, zone choice, CSR content,
TXT create/wait/delete, and the result's validation. The live run against
Let's Encrypt production is the gate's dev verification.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from custom_components.kiosk_satellite_manager import acme_issuer, dns_cloudflare, le_certificate
from custom_components.kiosk_satellite_manager.const import (
    CONF_ACME_ACCOUNT_KEY, CONF_ACME_ACCOUNT_URL, CONF_ACME_DNS_TOKEN,
)

TOKEN = "cf-token-never-shown"
SETTINGS = {CONF_ACME_DNS_TOKEN: TOKEN, CONF_ACME_ACCOUNT_KEY: "account-key-pem",
            CONF_ACME_ACCOUNT_URL: "https://acme.test/acct/1"}
ZONES = [
    dns_cloudflare.Zone("z-root", "cfoxga.com", ("a.ns.test", "b.ns.test")),
    dns_cloudflare.Zone("z-sub", "lab.cfoxga.com", ("c.ns.test",)),
    dns_cloudflare.Zone("z-other", "example.org", ("d.ns.test",)),
]


def _ca():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test ACME CA")])
    return key, name


class FakeAcme:
    """The `acme_issuer._protocol` seam: records calls, signs the CSR."""

    def __init__(self, *, finalize_error: Exception | None = None):
        self.ca_key, self.ca_name = _ca()
        self.csrs: list[x509.CertificateSigningRequest] = []
        self.answered: list[str] = []
        self.accounts: list[tuple] = []
        self.finalize_error = finalize_error

    def __call__(self, account_key_pem, account_url):
        self.accounts.append((account_key_pem, account_url))
        return self

    def register(self, email):
        return "https://acme.test/acct/9"

    def new_order(self, csr_pem):
        csr = x509.load_pem_x509_csr(csr_pem)
        self.csrs.append(csr)
        host = csr.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)[0]
        return csr, [(f"_acme-challenge.{host}", "txt-value", f"challb-{host}")]

    def answer(self, challb):
        self.answered.append(challb)

    def finalize(self, order, timeout_s):
        if self.finalize_error is not None:
            raise self.finalize_error
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(order.subject).issuer_name(self.ca_name)
            .public_key(order.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=90))
            .add_extension(order.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value, critical=False)
            .sign(self.ca_key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture
def cloudflare():
    created: list[tuple] = []
    deleted: list[tuple] = []

    async def create(_session, token, zone_id, name, value):
        assert token == TOKEN
        created.append((zone_id, name, value))
        return f"rec-{len(created)}"

    async def delete(_session, token, zone_id, record_id):
        deleted.append((zone_id, record_id))

    with patch.object(dns_cloudflare, "async_zones", new=AsyncMock(return_value=ZONES)), \
            patch.object(dns_cloudflare, "async_create_txt", new=AsyncMock(side_effect=create)), \
            patch.object(dns_cloudflare, "async_delete_txt", new=AsyncMock(side_effect=delete)), \
            patch.object(acme_issuer, "_ns_ips", return_value=["198.51.100.1"]) as ns, \
            patch.object(acme_issuer, "_txt_visible", return_value=True) as visible, \
            patch.object(acme_issuer, "TXT_POLL_S", 0):
        yield {"created": created, "deleted": deleted, "ns": ns, "visible": visible}


async def test_issue_one_name_with_its_own_key(hass, cloudflare):
    """The CSR's only SAN is the name, the longest zone is used, the TXT is
    removed, and the result validates against the returned key."""
    fake = FakeAcme()
    with patch.object(acme_issuer, "_protocol", new=fake):
        material = await acme_issuer.async_issue(hass, SETTINGS, "Portal.Lab.cfoxga.com.")
    csr = fake.csrs[0]
    sans = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sans.get_values_for_type(x509.DNSName) == ["portal.lab.cfoxga.com"]
    assert csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "portal.lab.cfoxga.com"
    assert fake.accounts == [("account-key-pem", "https://acme.test/acct/1")]
    assert cloudflare["created"] == [("z-sub", "_acme-challenge.portal.lab.cfoxga.com", "txt-value")]
    assert cloudflare["deleted"] == [("z-sub", "rec-1")]
    assert cloudflare["ns"].call_args.args[0] == ("c.ns.test",)
    assert fake.answered == ["challb-portal.lab.cfoxga.com"]
    assert material.not_after is not None
    again = le_certificate.validate(material.certificate.encode(), material.private_key.encode(),
                                    "portal.lab.cfoxga.com", exact=True)
    assert again.spki_sha256 == material.spki_sha256


async def test_given_key_is_kept_and_new_keys_differ(hass, cloudflare):
    """[KSM-TEST-410] A renewal keeps the key (stable pin); two new devices get two keys."""
    with patch.object(acme_issuer, "_protocol", new=FakeAcme()):
        first = await acme_issuer.async_issue(hass, SETTINGS, "one.cfoxga.com")
        second = await acme_issuer.async_issue(hass, SETTINGS, "two.cfoxga.com")
        renewed = await acme_issuer.async_issue(hass, SETTINGS, "one.cfoxga.com", first.private_key)
    assert first.private_key != second.private_key
    assert first.spki_sha256 != second.spki_sha256
    assert renewed.private_key == first.private_key
    assert renewed.spki_sha256 == first.spki_sha256


@pytest.mark.parametrize("hostname", ["192.0.2.10", "portal.local", "portal.elsewhere.net"])
async def test_bad_names_fail_before_any_order(hass, cloudflare, hostname):
    """Negative: an IP, a `.local` name and a name in no zone never reach ACME."""
    fake = FakeAcme()
    with patch.object(acme_issuer, "_protocol", new=fake):
        with pytest.raises(le_certificate.CertificateUnavailable):
            await acme_issuer.async_issue(hass, SETTINGS, hostname)
    assert fake.csrs == []
    assert cloudflare["created"] == []


async def test_propagation_timeout_still_deletes_the_record(hass, cloudflare):
    """Negative: the TXT never appears at the authoritative servers."""
    cloudflare["visible"].return_value = False
    fake = FakeAcme()
    with patch.object(acme_issuer, "_protocol", new=fake), patch.object(acme_issuer, "TXT_WAIT_S", 0):
        with pytest.raises(le_certificate.CertificateUnavailable, match="did not appear"):
            await acme_issuer.async_issue(hass, SETTINGS, "portal.cfoxga.com")
    assert fake.answered == []
    assert cloudflare["deleted"] == [("z-root", "rec-1")]


async def test_invalid_challenge_still_deletes_and_never_leaks(hass, cloudflare, caplog):
    """Negative / [KSM-TEST-403]: an ACME error whose text holds the token
    becomes a fixed message; the TXT is still removed; logs stay clean."""
    caplog.set_level(logging.DEBUG)
    fake = FakeAcme(finalize_error=RuntimeError(f"urn:acme:unauthorized {TOKEN} account-key-pem"))
    with patch.object(acme_issuer, "_protocol", new=fake):
        with pytest.raises(le_certificate.CertificateUnavailable) as raised:
            await acme_issuer.async_issue(hass, SETTINGS, "portal.cfoxga.com")
    assert str(raised.value) == "Let's Encrypt did not issue the certificate"
    assert cloudflare["deleted"] == [("z-root", "rec-1")]
    assert TOKEN not in caplog.text and "account-key-pem" not in caplog.text


async def test_cloudflare_error_is_a_fixed_message(hass, cloudflare):
    """Negative: a refused TXT create maps to a fixed step message."""
    with patch.object(acme_issuer, "_protocol", new=FakeAcme()), patch.object(
        dns_cloudflare, "async_create_txt",
        new=AsyncMock(side_effect=dns_cloudflare.CloudflareError("api_error")),
    ):
        with pytest.raises(le_certificate.CertificateUnavailable,
                           match="Cloudflare refused the DNS change"):
            await acme_issuer.async_issue(hass, SETTINGS, "portal.cfoxga.com")
    assert cloudflare["deleted"] == []


async def test_register_hides_library_errors(hass, caplog):
    """[KSM-TEST-403] Registration failure text never reaches logs or errors."""
    caplog.set_level(logging.DEBUG)

    def broken(_key, _url):
        raise RuntimeError(f"directory said {TOKEN}")

    with patch.object(acme_issuer, "_protocol", new=broken):
        with pytest.raises(le_certificate.CertificateUnavailable) as raised:
            await acme_issuer.async_register(hass, "ops@example.com")
    assert TOKEN not in str(raised.value) and TOKEN not in caplog.text
    with patch.object(acme_issuer, "_protocol", new=FakeAcme()):
        key, url = await acme_issuer.async_register(hass, "ops@example.com")
    assert url == "https://acme.test/acct/9"
    assert "PRIVATE KEY" in key


async def test_waits_for_the_record_and_a_failed_delete_is_only_logged(hass, cloudflare, caplog):
    """The TXT appears on the second check; a refused delete logs its code only."""
    cloudflare["visible"].side_effect = [False, True]
    fake = FakeAcme()
    with patch.object(acme_issuer, "_protocol", new=fake), patch.object(
        dns_cloudflare, "async_delete_txt",
        new=AsyncMock(side_effect=dns_cloudflare.CloudflareError("api_error")),
    ):
        material = await acme_issuer.async_issue(hass, SETTINGS, "portal.cfoxga.com")
    assert material.not_after is not None
    assert cloudflare["visible"].call_count == 2
    assert "Could not remove an ACME TXT record: api_error" in caplog.text


async def test_register_without_an_account_url_fails(hass):
    """Negative: a registration with no account URL, or the adapter's own
    fixed error, is a registration failure."""
    fake = FakeAcme()
    fake.register = lambda _email: None
    with patch.object(acme_issuer, "_protocol", new=fake):
        with pytest.raises(le_certificate.CertificateUnavailable, match="registration failed"):
            await acme_issuer.async_register(hass, "ops@example.com")

    def refuse(_key, _url):
        raise le_certificate.CertificateUnavailable("Let's Encrypt offered no DNS challenge")
    with patch.object(acme_issuer, "_protocol", new=refuse):
        with pytest.raises(le_certificate.CertificateUnavailable, match="no DNS challenge"):
            await acme_issuer.async_register(hass, "ops@example.com")
