"""Validate a certificate for a KS device: HA's legacy `/ssl` add-on files
(KSM-BEHAVE-179) or one KSM issued itself (KSM-BEHAVE-204).

This module deliberately has no Home Assistant imports so validation is also
usable in a small isolated test. It never logs PEM material or an exception
that might include file content.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtensionOID


class CertificateUnavailable(Exception):
    """The selected HA certificate cannot be used for the device."""


class HostnameNotCovered(CertificateUnavailable):
    """Valid HA certificate, but its DNS SAN lacks the Portal hostname (KSM-BEHAVE-182)."""


class CertificateMaterial(NamedTuple):
    certificate: str
    private_key: str
    spki_sha256: str
    fingerprint: str
    not_after: datetime | None = None


_DNS_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$", re.I)


def _covers(pattern: str, hostname: str) -> bool:
    pattern = pattern.lower()
    hostname = hostname.lower()
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return hostname.endswith("." + suffix) and hostname.count(".") == suffix.count(".") + 1
    return pattern == hostname


def check_hostname(hostname: str) -> str:
    """A normalized public DNS name, never an IP or a `.local` name."""
    hostname = hostname.strip().rstrip(".").lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise CertificateUnavailable("Enter a Portal DNS hostname, not its IP address")
    if not _DNS_NAME.fullmatch(hostname) or hostname.endswith(".local"):
        raise CertificateUnavailable("Enter a public DNS hostname for the Portal")
    return hostname


def load_for_hostname(hostname: str, directory: Path = Path("/ssl")) -> CertificateMaterial:
    """Require an unexpired cert for the Portal DNS name and its matching key."""
    hostname = check_hostname(hostname)
    try:
        cert_bytes = (directory / "fullchain.pem").read_bytes()
        key_bytes = (directory / "privkey.pem").read_bytes()
    except OSError:
        raise CertificateUnavailable("HA Let's Encrypt certificate files are unavailable in /ssl") from None
    return validate(cert_bytes, key_bytes, hostname)


def validate(
    cert_bytes: bytes, key_bytes: bytes, hostname: str, *, exact: bool = False
) -> CertificateMaterial:
    """Check a certificate chain and key for `hostname`.

    `exact` (KSM-BEHAVE-204) also requires the leaf's DNS SANs to be exactly
    that one name, as KSM orders them."""
    if len(cert_bytes) > 1024 * 1024 or len(key_bytes) > 64 * 1024:
        raise CertificateUnavailable("Certificate files exceed the expected size")
    try:
        certificate_text = cert_bytes.decode("ascii")
        private_key_text = key_bytes.decode("ascii")
        cert = x509.load_pem_x509_certificate(cert_bytes)
        key = serialization.load_pem_private_key(key_bytes, password=None)
        names = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        ).value.get_values_for_type(x509.DNSName)
    except (UnicodeError, ValueError, TypeError, UnsupportedAlgorithm, x509.ExtensionNotFound):
        raise CertificateUnavailable("Certificate or private key is invalid") from None
    if not isinstance(key, (ec.EllipticCurvePrivateKey, rsa.RSAPrivateKey)):
        raise CertificateUnavailable("Certificate uses a key type KS cannot import")
    now = datetime.now(timezone.utc)
    if hasattr(cert, "not_valid_before_utc"):
        before, after = cert.not_valid_before_utc, cert.not_valid_after_utc
    else:
        before = cert.not_valid_before.replace(tzinfo=timezone.utc)
        after = cert.not_valid_after.replace(tzinfo=timezone.utc)
    if not (before <= now < after):
        raise CertificateUnavailable("Certificate is not currently valid")
    if exact and [name.lower() for name in names] != [hostname]:
        raise CertificateUnavailable("Certificate does not name exactly this Portal hostname")
    if not any(_covers(name, hostname) for name in names):
        raise HostnameNotCovered("Certificate does not cover this Portal hostname")
    try:
        cert_key = cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        private_public = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    except (ValueError, TypeError):
        raise CertificateUnavailable("Certificate private key is invalid") from None
    if cert_key != private_public:
        raise CertificateUnavailable("Certificate and private key do not match")
    return CertificateMaterial(
        certificate_text, private_key_text,
        hashlib.sha256(cert_key).hexdigest(), cert.fingerprint(hashes.SHA256()).hex(), after,
    )
