"""Establish and pin HTTPS on a Kiosk Satellite device (KSM-BEHAVE-094, #57).

KS 2026.9.78+ can serve its management port (:2324) over HTTPS with a
self-signed device certificate (per-device `remote.tls`, default off). This
module switches a device to HTTPS and returns the SPKI SHA-256 to pin, or
switches a pinned device back to HTTP; HTTPS is operator opt-in (#137,
KSM-BEHAVE-169). The pinned transport itself lives in `ks_api_client`
(KSM-BEHAVE-093).
"""
from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

from . import ks_api_client
from .ks_api_client import KsApiError
from .le_certificate import CertificateMaterial

_LOGGER = logging.getLogger(__name__)

MIN_TLS_VERSION = (2026, 9, 78)
# KS restarts its listener ~750 ms after the setting changes (debounced).
TLS_ENABLE_POLL_ATTEMPTS = 10
TLS_ENABLE_POLL_DELAY_S = 1.0
# An import restarts the listener with a new key; a Portal can take well past
# the setting debounce to come back (KSM-BEHAVE-180, #175).
CERT_IMPORT_POLL_ATTEMPTS = 30

_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)")


def _parse_version(value: object) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(value) if isinstance(value, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


async def async_establish_tls(
    session: aiohttp.ClientSession, host: str, password: str
) -> str | None:
    """Return the pin for `host`, switching it to HTTPS first if needed, or
    None when the device's KS cannot serve TLS (it stays on HTTP)."""
    probed = await ks_api_client.probe_https(session, host)
    if probed is not None:
        # Already serving HTTPS: trust this key on first use.
        return probed[0]

    health = await ks_api_client.get_health(session, host, pin=None)
    version = _parse_version(health.get("appVersion"))
    if version is None or version < MIN_TLS_VERSION:
        _LOGGER.warning(
            "Kiosk Satellite at %s reports version %r; HTTPS needs %s or later, "
            "so management stays on HTTP",
            host,
            health.get("appVersion"),
            ".".join(map(str, MIN_TLS_VERSION)),
        )
        return None

    token = await ks_api_client.login(session, host, password, pin=None)
    result = await ks_api_client.run_command(session, host, token, "tlsCertificate", pin=None)
    certificate = (result.get("data") or {}).get("certificate") if result.get("ok") else None
    if not certificate:
        raise KsApiError(f"tlsCertificate returned no certificate: {result.get('error')!r}")
    expected = ks_api_client.spki_sha256_from_pem(certificate)
    await ks_api_client.patch_settings(session, host, token, {"remote.tls": True}, pin=None)

    for _ in range(TLS_ENABLE_POLL_ATTEMPTS):
        await asyncio.sleep(TLS_ENABLE_POLL_DELAY_S)
        probed = await ks_api_client.probe_https(session, host)
        if probed is None:
            continue
        if probed[0] != expected:
            raise KsApiError(
                f"{host} served a different TLS key after enabling HTTPS; nothing pinned"
            )
        return expected
    raise KsApiError(f"{host} did not answer over HTTPS after enabling remote.tls")


async def async_disable_tls(
    session: aiohttp.ClientSession, host: str, password: str, pin: str
) -> None:
    """Switch a pinned device back to plaintext HTTP (KSM-BEHAVE-169).

    The login and the settings write travel over the pinned channel; this
    returns only once the device answers over HTTP, so the caller drops the
    pin knowing the device is reachable without it."""
    token = await ks_api_client.login(session, host, password, pin=pin)
    await ks_api_client.patch_settings(session, host, token, {"remote.tls": False}, pin=pin)
    for _ in range(TLS_ENABLE_POLL_ATTEMPTS):
        await asyncio.sleep(TLS_ENABLE_POLL_DELAY_S)
        try:
            await ks_api_client.get_health(session, host, pin=None)
        except (KsApiError, aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            continue
        return
    raise KsApiError(f"{host} did not answer over HTTP after disabling remote.tls")


async def async_import_certificate(
    session: aiohttp.ClientSession, host: str, password: str, pin: str,
    material: CertificateMaterial,
) -> str:
    """Import HA's certificate using only the currently pinned HTTPS channel.

    KS restarts its listener after import. The unauthenticated probe then
    confirms the exact public key we selected before trusting that new key.
    """
    token = await ks_api_client.login(session, host, password, pin=pin)
    try:
        result = await ks_api_client.run_command(
            session, host, token, "importTlsCertificate", pin=pin,
            params={"certificate": material.certificate, "privateKey": material.private_key},
        )
    except KsApiError:
        raise KsApiError("Kiosk Satellite rejected the HA certificate") from None
    if not result.get("ok"):
        raise KsApiError("Kiosk Satellite rejected the HA certificate")
    for _ in range(CERT_IMPORT_POLL_ATTEMPTS):
        await asyncio.sleep(TLS_ENABLE_POLL_DELAY_S)
        probed = await ks_api_client.probe_https_identity(session, host)
        if probed is None:
            continue
        if probed == (material.spki_sha256, material.fingerprint):
            return material.spki_sha256
    raise KsApiError("Kiosk Satellite did not serve the imported certificate and key")
