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
