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
