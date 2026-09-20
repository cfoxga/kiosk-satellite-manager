"""Fail-closed signer policy for downloaded Kiosk Satellite APKs.

Android's Package Manager cryptographically validates APK signatures during
``pm install``. This module extracts the certificate identity carried in an
APK v2/v3 signing block before KSM performs any device mutation, so an
incompatible-update result can never turn into an uninstall-and-fresh-install
bypass of Android's signer-continuity protection.
"""
from __future__ import annotations

import hashlib
import struct
from typing import Final

_APK_SIG_BLOCK_MAGIC: Final = b"APK Sig Block 42"
_APK_SIG_V2_ID: Final = 0x7109871A
_APK_SIG_V3_ID: Final = 0xF05368C0

# Leaf certificate SHA-256 from the published Kiosk Satellite 2026.9.67 APK.
# A legitimate upstream signing-key rotation must be reviewed and added here;
# downloaded artifacts and device state are never trust sources.
TRUSTED_KS_CERTIFICATE_SHA256: Final = frozenset(
    {"2a77134ce11f65ba9d08bed669fdcb742a25633c5665a6135aeca30040b068fc"}
)


class ApkSignerVerificationFailed(ValueError):
    """An APK has no supported signer identity or it is not trusted by KSM."""


def _length_prefixed(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(data):
        raise ApkSignerVerificationFailed("truncated length-prefixed APK signing field")
    length = struct.unpack_from("<I", data, offset)[0]
    start = offset + 4
    end = start + length
    if end > len(data):
        raise ApkSignerVerificationFailed("invalid length-prefixed APK signing field")
    return data[start:end], end


def _apk_signing_block(apk: bytes) -> bytes:
    # The EOCD is at most 65,557 bytes from EOF (the ZIP comment is uint16).
    search_start = max(0, len(apk) - 65_557)
    eocd = apk.rfind(b"PK\x05\x06", search_start)
    if eocd < 0 or eocd + 22 > len(apk):
        raise ApkSignerVerificationFailed("APK ZIP end-of-central-directory missing")
    comment_length = struct.unpack_from("<H", apk, eocd + 20)[0]
    if eocd + 22 + comment_length != len(apk):
        raise ApkSignerVerificationFailed("invalid APK ZIP end-of-central-directory")
    central_directory_offset = struct.unpack_from("<I", apk, eocd + 16)[0]
    if central_directory_offset == 0xFFFFFFFF or central_directory_offset < 24:
        raise ApkSignerVerificationFailed("unsupported APK ZIP central-directory offset")
    footer_offset = central_directory_offset - 24
    size, magic = struct.unpack_from("<Q16s", apk, footer_offset)
    if magic != _APK_SIG_BLOCK_MAGIC or size < 24 or size + 8 > central_directory_offset:
        raise ApkSignerVerificationFailed("APK Signing Block missing or invalid")
    block_start = central_directory_offset - (size + 8)
    block = apk[block_start:central_directory_offset]
    if struct.unpack_from("<Q", block, 0)[0] != size:
        raise ApkSignerVerificationFailed("APK Signing Block size mismatch")
    return block


def apk_signer_fingerprints(apk: bytes) -> frozenset[str]:
    """Return SHA-256 leaf-certificate fingerprints from APK v2/v3 blocks.

    The Package Manager remains the signature validator. This parser only
    obtains the signer identity that KSM pins before it lets a downloaded APK
    reach the Package Manager or a destructive recovery branch.
    """
    block = _apk_signing_block(apk)
    fingerprints: set[str] = set()
    offset, end = 8, len(block) - 24
    while offset < end:
        if offset + 8 > end:
            raise ApkSignerVerificationFailed("truncated APK Signing Block pair")
        pair_length = struct.unpack_from("<Q", block, offset)[0]
        offset += 8
        pair_end = offset + pair_length
        if pair_length < 4 or pair_end > end:
            raise ApkSignerVerificationFailed("invalid APK Signing Block pair")
        pair = block[offset:pair_end]
        offset = pair_end
        scheme_id = struct.unpack_from("<I", pair, 0)[0]
        if scheme_id not in (_APK_SIG_V2_ID, _APK_SIG_V3_ID):
            continue
        signers, _ = _length_prefixed(pair, 4)
        signer_offset = 0
        while signer_offset < len(signers):
            signer, signer_offset = _length_prefixed(signers, signer_offset)
            signed_data, signed_data_offset = _length_prefixed(signer, 0)
            _, signed_data_offset = _length_prefixed(signed_data, 0)  # digests
            certificates, _ = _length_prefixed(signed_data, signed_data_offset)
            certificate_offset = 0
            while certificate_offset < len(certificates):
                certificate, certificate_offset = _length_prefixed(certificates, certificate_offset)
                if not certificate:
                    raise ApkSignerVerificationFailed("empty APK signing certificate")
                fingerprints.add(hashlib.sha256(certificate).hexdigest())
    if not fingerprints:
        raise ApkSignerVerificationFailed("APK has no v2/v3 signing certificate")
    return frozenset(fingerprints)


def verify_ks_apk_signer(apk: bytes) -> None:
    """Raise unless every APK signer belongs to the reviewed KSM policy."""
    fingerprints = apk_signer_fingerprints(apk)
    if not fingerprints.issubset(TRUSTED_KS_CERTIFICATE_SHA256):
        raise ApkSignerVerificationFailed("APK signer is not trusted by KSM policy")
