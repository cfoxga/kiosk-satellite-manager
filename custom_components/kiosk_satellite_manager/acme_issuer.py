"""KSM's own Let's Encrypt issuer over ACME DNS-01 (KSM-BEHAVE-204, #200).

One certificate per device: a single DNS SAN and the device's own EC P-256
key, which renewals reuse so the device pin stays stable. The challenge TXT
record goes through Cloudflare (`dns_cloudflare`) and is always removed.
Errors are fixed `CertificateUnavailable` messages; logs carry only an
exception type name, never a token, key, PEM or server response.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

import dns.message
import dns.query
import dns.rdatatype
import dns.resolver
import josepy as jose
from acme import challenges, client as acme_client, messages
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import dns_cloudflare
from .const import (
    ACME_DIRECTORY_URL,
    CONF_ACME_ACCOUNT_KEY,
    CONF_ACME_ACCOUNT_URL,
    CONF_ACME_DNS_TOKEN,
    DOMAIN,
)
from .le_certificate import (
    CertificateMaterial,
    CertificateUnavailable,
    check_hostname,
    validate,
)

_LOGGER = logging.getLogger(__name__)
_LOCK_KEY = f"{DOMAIN}_acme_lock"
USER_AGENT = "kiosk-satellite-manager"
TXT_WAIT_S = 120
TXT_POLL_S = 5
ORDER_TIMEOUT_S = 180

_CLOUDFLARE_MESSAGES = {
    "invalid_token": "The Cloudflare API token was rejected",
    "cannot_connect": "Cloudflare could not be reached",
    "api_error": "Cloudflare refused the DNS change",
}


def new_key_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _csr_pem(key_pem: str, hostname: str) -> bytes:
    key = serialization.load_pem_private_key(key_pem.encode("ascii"), password=None)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM)


class AcmeProtocol:
    """Blocking wrapper over the `acme` library; call only in the executor."""

    def __init__(self, account_key_pem: str, account_url: str | None) -> None:
        self._key = jose.JWKEC(key=serialization.load_pem_private_key(
            account_key_pem.encode("ascii"), password=None))
        account = (
            messages.RegistrationResource(body=messages.Registration(), uri=account_url)
            if account_url else None
        )
        net = acme_client.ClientNetwork(
            self._key, account=account, alg=jose.ES256, user_agent=USER_AGENT)
        self._client = acme_client.ClientV2(
            acme_client.ClientV2.get_directory(ACME_DIRECTORY_URL, net), net)

    def register(self, email: str) -> str:
        regr = self._client.new_account(messages.NewRegistration.from_data(
            email=email, terms_of_service_agreed=True))
        return regr.uri

    def new_order(self, csr_pem: bytes) -> tuple[Any, list[tuple[str, str, Any]]]:
        """The order and one (TXT name, TXT value, challenge) per pending authz."""
        order = self._client.new_order(csr_pem)
        pending = []
        for authz in order.authorizations:
            if authz.body.status == messages.STATUS_VALID:
                continue
            domain = authz.body.identifier.value
            challb = next((c for c in authz.body.challenges
                           if isinstance(c.chall, challenges.DNS01)), None)
            if challb is None:
                raise CertificateUnavailable("Let's Encrypt offered no DNS challenge")
            pending.append((challb.chall.validation_domain_name(domain),
                            challb.chall.validation(self._key), challb))
        return order, pending

    def answer(self, challb: Any) -> None:
        self._client.answer_challenge(challb, challb.chall.response(self._key))

    def finalize(self, order: Any, timeout_s: float) -> str:
        deadline = dt.datetime.now() + dt.timedelta(seconds=timeout_s)
        return self._client.poll_and_finalize(order, deadline).fullchain_pem


def _protocol(account_key_pem: str, account_url: str | None) -> AcmeProtocol:
    """Test seam: the ACME protocol adapter (KSM-TEST-404)."""
    return AcmeProtocol(account_key_pem, account_url)


def _ns_ips(name_servers: tuple[str, ...]) -> list[str]:
    ips: list[str] = []
    for server in name_servers:
        try:
            ips.extend(r.address for r in dns.resolver.resolve(server, "A", lifetime=5))
        except Exception:  # noqa: BLE001 -- one unresolvable NS is not fatal
            continue
    return ips


def _txt_visible(ips: list[str], name: str, value: str) -> bool:
    """True once every authoritative server answers `name` TXT with `value`."""
    if not ips:
        return False
    for ip in ips:
        try:
            answer = dns.query.udp(dns.message.make_query(name, dns.rdatatype.TXT), ip, timeout=5)
        except Exception:  # noqa: BLE001
            return False
        texts = {
            b"".join(item.strings).decode("ascii", "replace")
            for rrset in answer.answer if rrset.rdtype == dns.rdatatype.TXT
            for item in rrset
        }
        if value not in texts:
            return False
    return True


async def _wait_visible(hass: HomeAssistant, zone: dns_cloudflare.Zone,
                        records: list[tuple[str, str, Any]]) -> None:
    ips = await hass.async_add_executor_job(_ns_ips, zone.name_servers)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TXT_WAIT_S
    for name, value, _ in records:
        while not await hass.async_add_executor_job(_txt_visible, ips, name, value):
            if loop.time() >= deadline:
                raise CertificateUnavailable("The DNS challenge record did not appear in time")
            await asyncio.sleep(TXT_POLL_S)


def _cloudflare(err: dns_cloudflare.CloudflareError) -> CertificateUnavailable:
    return CertificateUnavailable(_CLOUDFLARE_MESSAGES.get(err.code, _CLOUDFLARE_MESSAGES["api_error"]))


async def async_register(hass: HomeAssistant, email: str) -> tuple[str, str]:
    """Create a Let's Encrypt account; returns (account key PEM, account URL)."""
    key_pem = new_key_pem()
    try:
        protocol = await hass.async_add_executor_job(_protocol, key_pem, None)
        url = await hass.async_add_executor_job(protocol.register, email)
    except CertificateUnavailable:
        raise
    except Exception as err:  # noqa: BLE001 -- message may hold server text
        _LOGGER.warning("Let's Encrypt account registration failed: %s", type(err).__name__)
        raise CertificateUnavailable("Let's Encrypt account registration failed") from None
    if not url:
        raise CertificateUnavailable("Let's Encrypt account registration failed")
    return key_pem, url


async def async_issue(
    hass: HomeAssistant, settings: dict[str, Any], hostname: str,
    device_key_pem: str | None = None,
) -> CertificateMaterial:
    """Issue a certificate naming exactly `hostname`, keyed by `device_key_pem`
    (a new key when None). One issuance at a time per HA instance."""
    hostname = check_hostname(hostname)
    key_pem = device_key_pem or new_key_pem()
    session = async_get_clientsession(hass)
    token = settings[CONF_ACME_DNS_TOKEN]
    lock = hass.data.setdefault(_LOCK_KEY, asyncio.Lock())
    async with lock:
        created: list[str] = []
        zone = None
        try:
            zone = dns_cloudflare.find_zone(
                await dns_cloudflare.async_zones(session, token), hostname)
            if zone is None:
                raise CertificateUnavailable("No Cloudflare zone holds this hostname")
            protocol = await hass.async_add_executor_job(
                _protocol, settings[CONF_ACME_ACCOUNT_KEY], settings.get(CONF_ACME_ACCOUNT_URL))
            order, records = await hass.async_add_executor_job(
                protocol.new_order, _csr_pem(key_pem, hostname))
            for name, value, _ in records:
                created.append(await dns_cloudflare.async_create_txt(
                    session, token, zone.id, name, value))
            await _wait_visible(hass, zone, records)
            for _, _, challb in records:
                await hass.async_add_executor_job(protocol.answer, challb)
            fullchain = await hass.async_add_executor_job(
                protocol.finalize, order, ORDER_TIMEOUT_S)
        except CertificateUnavailable:
            raise
        except dns_cloudflare.CloudflareError as err:
            raise _cloudflare(err) from None
        except Exception as err:  # noqa: BLE001 -- message may hold server text
            _LOGGER.warning("Let's Encrypt issuance failed: %s", type(err).__name__)
            raise CertificateUnavailable("Let's Encrypt did not issue the certificate") from None
        finally:
            for record_id in created:
                try:
                    await dns_cloudflare.async_delete_txt(session, token, zone.id, record_id)
                except dns_cloudflare.CloudflareError as err:
                    _LOGGER.warning("Could not remove an ACME TXT record: %s", err.code)
    return validate(fullchain.encode("ascii"), key_pem.encode("ascii"), hostname, exact=True)
