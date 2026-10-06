"""[KSM-TEST-352] Validate HA add-on material before touching a device."""

from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


SOURCE = Path(__file__).parents[2] / "custom_components/kiosk_satellite_manager/le_certificate.py"
spec = spec_from_file_location("ksm_le_certificate", SOURCE)
module = module_from_spec(spec)
spec.loader.exec_module(module)


def material(*, names=("test-portal-mini.cfoxga.com",), expired=False):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic CA")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=10))
        .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()),
    )


def test_valid_certificate_for_portal_name(tmp_path):
    certificate, key = material()
    (tmp_path / "fullchain.pem").write_bytes(certificate)
    (tmp_path / "privkey.pem").write_bytes(key)
    selected = module.load_for_hostname("test-portal-mini.cfoxga.com", tmp_path)
    assert selected.certificate.encode() == certificate
    assert selected.private_key.encode() == key
    assert len(selected.spki_sha256) == 64
    assert len(selected.fingerprint) == 64


def test_wildcard_covers_only_one_dns_label(tmp_path):
    certificate, key = material(names=("*.cfoxga.com",))
    (tmp_path / "fullchain.pem").write_bytes(certificate)
    (tmp_path / "privkey.pem").write_bytes(key)
    assert module.load_for_hostname("test-portal-mini.cfoxga.com", tmp_path)
    with pytest.raises(module.CertificateUnavailable):
        module.load_for_hostname("nested.test-portal-mini.cfoxga.com", tmp_path)


@pytest.mark.parametrize("hostname", ["192.168.40.5", "other.cfoxga.com", "", "portal.local"])
def test_wrong_hostname_never_accepts_material(tmp_path, hostname):
    certificate, key = material()
    (tmp_path / "fullchain.pem").write_bytes(certificate)
    (tmp_path / "privkey.pem").write_bytes(key)
    with pytest.raises(module.CertificateUnavailable):
        module.load_for_hostname(hostname, tmp_path)


def test_missing_mismatched_or_expired_material_rejected(tmp_path):
    with pytest.raises(module.CertificateUnavailable):
        module.load_for_hostname("test-portal-mini.cfoxga.com", tmp_path)
    certificate, _ = material(expired=True)
    _, key = material()
    (tmp_path / "fullchain.pem").write_bytes(certificate)
    (tmp_path / "privkey.pem").write_bytes(key)
    with pytest.raises(module.CertificateUnavailable):
        module.load_for_hostname("test-portal-mini.cfoxga.com", tmp_path)
    certificate, _ = material()
    (tmp_path / "fullchain.pem").write_bytes(certificate)
    with pytest.raises(module.CertificateUnavailable):
        module.load_for_hostname("test-portal-mini.cfoxga.com", tmp_path)


def _write(tmp_path, certificate: bytes, key: bytes) -> None:
    (tmp_path / "fullchain.pem").write_bytes(certificate)
    (tmp_path / "privkey.pem").write_bytes(key)


def test_oversized_and_garbage_files_rejected(tmp_path):
    _write(tmp_path, b"x" * (1024 * 1024 + 1), b"k")
    with pytest.raises(module.CertificateUnavailable, match="exceed the expected size"):
        module.load_for_hostname("test-portal-mini.cfoxga.com", tmp_path)
    _write(tmp_path, b"not a certificate", b"not a key")
    with pytest.raises(module.CertificateUnavailable, match="invalid"):
        module.load_for_hostname("test-portal-mini.cfoxga.com", tmp_path)


def test_key_type_ks_cannot_import_is_rejected(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    certificate, _ = material()
    key = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _write(tmp_path, certificate, key)
    with pytest.raises(module.CertificateUnavailable, match="key type KS cannot import"):
        module.load_for_hostname("test-portal-mini.cfoxga.com", tmp_path)


def test_exact_validation_requires_the_name_alone():
    """[KSM-TEST-404] A KSM-issued certificate must name exactly its hostname:
    an extra SAN or a wildcard fails `exact`, a single matching SAN passes."""
    host = "test-portal-mini.cfoxga.com"
    single = module.validate(*material(names=(host,)), host, exact=True)
    assert single.not_after > datetime.now(timezone.utc)
    for names in ((host, "ha.cfoxga.com"), ("*.cfoxga.com",)):
        with pytest.raises(module.CertificateUnavailable, match="exactly"):
            module.validate(*material(names=names), host, exact=True)
    assert module.validate(*material(names=(host, "ha.cfoxga.com")), host).spki_sha256


@pytest.mark.parametrize("bad", ["192.0.2.10", "portal.local", "no_dots", ""])
def test_check_hostname_rejects_what_acme_cannot_issue(bad):
    """[KSM-TEST-404] Negative: an IP, a `.local` name and a non-DNS name fail."""
    with pytest.raises(module.CertificateUnavailable):
        module.check_hostname(bad)
    assert module.check_hostname(" Portal.CFoxGA.com. ") == "portal.cfoxga.com"
