"""[KSM-TEST-098] APK signer-policy tests without device-side mutation."""
from __future__ import annotations

import hashlib
import struct

import pytest

from custom_components.kiosk_satellite_manager.apk_signing import (
    ApkSignerVerificationFailed,
    apk_signer_fingerprints,
    verify_ks_apk_signer,
)


def _lp(value: bytes) -> bytes:
    return struct.pack("<I", len(value)) + value


def _signed_apk_fixture(certificate: bytes) -> bytes:
    """Small APK-shaped v2 block fixture; PM crypto validation is Android's job."""
    signed_data = _lp(b"") + _lp(_lp(certificate))
    signer = _lp(signed_data) + _lp(b"") + _lp(b"")
    # The v2 scheme payload is a length-prefixed sequence of length-prefixed
    # signers, matching the nested structure in a real APK Signing Block.
    pair = struct.pack("<I", 0x7109871A) + _lp(_lp(signer))
    pairs = struct.pack("<Q", len(pair)) + pair
    size = len(pairs) + 24
    block = struct.pack("<Q", size) + pairs + struct.pack("<Q", size) + b"APK Sig Block 42"
    central_directory_offset = len(block)
    eocd = b"PK\x05\x06" + (b"\0" * 12) + struct.pack("<I", central_directory_offset) + b"\0\0"
    return block + eocd


def test_apk_signer_fingerprints_extracts_v2_leaf_certificate():
    certificate = b"reviewed signer certificate"
    assert apk_signer_fingerprints(_signed_apk_fixture(certificate)) == {
        hashlib.sha256(certificate).hexdigest()
    }


def test_verify_ks_apk_signer_rejects_untrusted_certificate():
    with pytest.raises(ApkSignerVerificationFailed, match="not trusted"):
        verify_ks_apk_signer(_signed_apk_fixture(b"attacker certificate"))


def test_verify_ks_apk_signer_accepts_policy_certificate(monkeypatch):
    certificate = b"reviewed signer certificate"
    monkeypatch.setattr(
        "custom_components.kiosk_satellite_manager.apk_signing.TRUSTED_KS_CERTIFICATE_SHA256",
        frozenset({hashlib.sha256(certificate).hexdigest()}),
    )
    verify_ks_apk_signer(_signed_apk_fixture(certificate))


def _apk_with_pairs(pairs: bytes) -> bytes:
    size = len(pairs) + 24
    block = struct.pack("<Q", size) + pairs + struct.pack("<Q", size) + b"APK Sig Block 42"
    eocd = b"PK\x05\x06" + (b"\0" * 12) + struct.pack("<I", len(block)) + b"\0\0"
    return block + eocd


def _pair(scheme_id: int, payload: bytes) -> bytes:
    pair = struct.pack("<I", scheme_id) + payload
    return struct.pack("<Q", len(pair)) + pair


def _fixture_parts(certificate: bytes = b"c") -> bytes:
    signed_data = _lp(b"") + _lp(_lp(certificate))
    return _lp(_lp(_lp(signed_data) + _lp(b"") + _lp(b"")))


def _tamper_eocd(apk: bytes, offset: int, packed: bytes) -> bytes:
    eocd = apk.rfind(b"PK\x05\x06")
    return apk[: eocd + offset] + packed + apk[eocd + offset + len(packed):]


def test_apk_signer_rejects_malformed_zip_and_block_structure():
    good = _signed_apk_fixture(b"c")
    with pytest.raises(ApkSignerVerificationFailed, match="end-of-central-directory missing"):
        apk_signer_fingerprints(b"not an apk at all, no eocd here")
    with pytest.raises(ApkSignerVerificationFailed, match="invalid APK ZIP end-of-central"):
        apk_signer_fingerprints(_tamper_eocd(good, 20, struct.pack("<H", 5)))
    with pytest.raises(ApkSignerVerificationFailed, match="unsupported APK ZIP central"):
        apk_signer_fingerprints(_tamper_eocd(good, 16, struct.pack("<I", 0xFFFFFFFF)))
    with pytest.raises(ApkSignerVerificationFailed, match="Signing Block missing or invalid"):
        apk_signer_fingerprints(good.replace(b"APK Sig Block 42", b"APK Sig Block 43"))
    # A header size that disagrees with the footer size.
    with pytest.raises(ApkSignerVerificationFailed, match="size mismatch"):
        apk_signer_fingerprints(struct.pack("<Q", 99) + good[8:])


def test_apk_signer_rejects_malformed_pairs_and_fields():
    with pytest.raises(ApkSignerVerificationFailed, match="truncated APK Signing Block pair"):
        apk_signer_fingerprints(_apk_with_pairs(b"\0" * 4))
    bad_length = struct.pack("<Q", 2) + b"\0\0"
    with pytest.raises(ApkSignerVerificationFailed, match="invalid APK Signing Block pair"):
        apk_signer_fingerprints(_apk_with_pairs(bad_length))
    with pytest.raises(ApkSignerVerificationFailed, match="no v2/v3"):
        apk_signer_fingerprints(_apk_with_pairs(_pair(0x1234, b"ignored")))
    with pytest.raises(ApkSignerVerificationFailed, match="truncated length-prefixed"):
        apk_signer_fingerprints(_apk_with_pairs(_pair(0x7109871A, b"\0")))
    with pytest.raises(ApkSignerVerificationFailed, match="invalid length-prefixed"):
        apk_signer_fingerprints(_apk_with_pairs(_pair(0x7109871A, struct.pack("<I", 500))))
    empty_cert = _lp(_lp(_lp(_lp(b"") + _lp(_lp(b"")) ) + _lp(b"") + _lp(b"")))
    with pytest.raises(ApkSignerVerificationFailed, match="empty APK signing certificate"):
        apk_signer_fingerprints(_apk_with_pairs(_pair(0x7109871A, empty_cert)))


def test_apk_signer_reads_v3_blocks():
    assert apk_signer_fingerprints(
        _apk_with_pairs(_pair(0xF05368C0, _fixture_parts(b"v3 cert")))
    ) == {hashlib.sha256(b"v3 cert").hexdigest()}
