"""Kiosk Satellite on-device web UI client (KSM-BEHAVE-010/011).

Endpoint shapes below were extracted live from the Test Portal's own served
JS bundles (main.js/core.js/wizard.js/settings.js at :2324/static/), not
guessed:

- `GET api/setup/status` (unauthenticated) -> {setupNeeded, passwordNeeded,
  deviceName, ...}.
- `POST api/setup/password` body {password, deviceName} (unauthenticated,
  only valid while passwordNeeded is true) -> {token, ...}. wizard.js's own
  device-rename step reuses this single call to set the admin password and
  Device Name together on first run.
- `POST /api/login` body {password} -> {token, ...} (unauthenticated).
- `GET /api/settings`, Bearer token -> {settings: [{key, value, ...}], ...}.
- `PATCH /api/settings` body {<key>: <value>, ...}, Bearer token -- used for
  both `device.name` (once a password already exists -- wizard.js's rename
  step falls back to this when !passwordNeeded) and `ha.url`/`ha.token`
  (wizard.js's Home Assistant step).
- `POST /api/commands/haCheckConnection` body "{}" , Bearer token ->
  {ok, error} -- validates whatever ha.url/ha.token are currently stored,
  used by both the wizard and the Settings tab's own "Validate" button.

Transport (KSM-BEHAVE-093, #57): every call takes a required keyword `pin`.
With a pin (SPKI SHA-256 hex) the call goes to https://<host>:2324 and the
TLS connection is refused before any request byte unless the served key
matches; with `pin=None` it keeps KSM-BEHAVE-070's fixed-origin HTTP.
"""
from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urlsplit

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .const import HEALTH_PORT, HEALTH_TIMEOUT_S


class KsApiError(Exception):
    """A Kiosk Satellite web-UI API call failed or was rejected."""


def spki_sha256_from_der(cert_der: bytes) -> str:
    """Hex SHA-256 of a DER certificate's SubjectPublicKeyInfo -- the pin.
    Keyed on the public key so KS's same-key renewal keeps matching."""
    cert = x509.load_der_x509_certificate(cert_der)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(spki).hexdigest()


def spki_sha256_from_pem(cert_pem: str) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return spki_sha256_from_der(cert.public_bytes(serialization.Encoding.DER))


def _peer_der(transport: asyncio.Transport) -> bytes:
    sslobj = transport.get_extra_info("ssl_object")
    der = sslobj.getpeercert(binary_form=True) if sslobj is not None else None
    if not der:
        raise aiohttp.ClientConnectionError("TLS peer presented no certificate")
    return der


class SpkiPin(aiohttp.Fingerprint):
    """aiohttp runs `check` right after the TLS handshake and before the
    request is written, so a key mismatch sends nothing (KSM-TEST-178).
    Using a Fingerprint also disables CA/hostname checks: the key is the
    identity."""

    def check(self, transport: asyncio.Transport) -> None:
        got = bytes.fromhex(spki_sha256_from_der(_peer_der(transport)))
        if got != self.fingerprint:
            host, port, *_ = transport.get_extra_info("peername")
            raise aiohttp.ServerFingerprintMismatch(self.fingerprint, got, host, port)


class _SpkiCapture(aiohttp.Fingerprint):
    """Accepts any key and records it -- only for unauthenticated probes
    (trust on first use / the operator-confirmed repair)."""

    def __init__(self) -> None:
        super().__init__(bytes(32))
        self.spki: str | None = None

    def check(self, transport: asyncio.Transport) -> None:
        self.spki = spki_sha256_from_der(_peer_der(transport))


# One SpkiPin per pin: the connector pools connections by the ssl object.
_PINS: dict[str, SpkiPin] = {}


def _ssl(pin: str | None) -> SpkiPin | bool:
    if pin is None:
        return True
    if pin not in _PINS:
        _PINS[pin] = SpkiPin(bytes.fromhex(pin))
    return _PINS[pin]


def _base_url(host: str, pin: str | None) -> str:
    scheme = "http" if pin is None else "https"
    return f"{scheme}://{host}:{HEALTH_PORT}"


def _credential_url(host: str, path: str, pin: str | None) -> str:
    """Bind credential requests to this device's management origin.

    Pinned entries go only to https (KSM-BEHAVE-093); unpinned entries keep
    the operator-approved HTTP compatibility path (KSM-BEHAVE-070). Either
    way the origin is the configured host and fixed port, and callers
    disable redirects so credentials cannot be forwarded onward.
    """
    base = urlsplit(_base_url(host, pin))
    expected_host = host.strip("[]").lower()
    if (
        base.scheme != ("http" if pin is None else "https")
        or base.hostname != expected_host
        or base.port != HEALTH_PORT
        or base.path not in ("", "/")
        or base.query
        or base.fragment
    ):
        raise KsApiError("credential request must target the configured device and management port")
    return f"{base.scheme}://{base.netloc}{path}"


async def probe_https(
    session: aiohttp.ClientSession, host: str, path: str = "/api/health"
) -> tuple[str, dict] | None:
    """Unauthenticated HTTPS GET that accepts any key and reports it:
    (served SPKI, JSON body), or None when nothing answers over TLS."""
    capture = _SpkiCapture()
    try:
        async with session.get(
            f"https://{host}:{HEALTH_PORT}{path}",
            ssl=capture,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
        ) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    if capture.spki is None:
        return None
    return capture.spki, body


async def get_health(session: aiohttp.ClientSession, host: str, *, pin: str | None) -> dict:
    async with session.get(
        f"{_base_url(host, pin)}/api/health",
        ssl=_ssl(pin),
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def get_setup_status(
    session: aiohttp.ClientSession, host: str, *, pin: str | None
) -> dict:
    async with session.get(
        f"{_base_url(host, pin)}/api/setup/status",
        ssl=_ssl(pin),
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def setup_password(
    session: aiohttp.ClientSession, host: str, password: str, device_name: str,
    *, pin: str | None,
) -> str:
    """First-run only (passwordNeeded is true): sets the admin password and
    Device Name together, returns the auth token."""
    async with session.post(
        _credential_url(host, "/api/setup/password", pin),
        json={"password": password, "deviceName": device_name},
        allow_redirects=False,
        ssl=_ssl(pin),
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        data = await resp.json()
        if not resp.ok:
            raise KsApiError(data.get("error") or f"HTTP {resp.status}")
        return data["token"]


async def login(
    session: aiohttp.ClientSession, host: str, password: str, *, pin: str | None
) -> str:
    async with session.post(
        _credential_url(host, "/api/login", pin),
        json={"password": password},
        allow_redirects=False,
        ssl=_ssl(pin),
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        data = await resp.json()
        if not resp.ok:
            raise KsApiError(data.get("error") or f"HTTP {resp.status}")
        return data["token"]


async def get_settings(
    session: aiohttp.ClientSession, host: str, token: str, *, pin: str | None
) -> dict:
    """`GET /api/settings`, Bearer token -> {key: current value}. The device
    describes every setting it has; only the values are kept (#56: the
    node name is readable here and nowhere unauthenticated)."""
    async with session.get(
        _credential_url(host, "/api/settings", pin),
        headers={"Authorization": f"Bearer {token}"},
        allow_redirects=False,
        ssl=_ssl(pin),
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        data = await resp.json()
        if not resp.ok:
            raise KsApiError(data.get("error") or f"HTTP {resp.status}")
        return {item["key"]: item.get("value") for item in data.get("settings") or []}


async def patch_settings(
    session: aiohttp.ClientSession, host: str, token: str, values: dict,
    *, pin: str | None,
) -> dict:
    async with session.patch(
        _credential_url(host, "/api/settings", pin),
        json=values,
        headers={"Authorization": f"Bearer {token}"},
        allow_redirects=False,
        ssl=_ssl(pin),
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        data = await resp.json()
        if not resp.ok:
            raise KsApiError(data.get("error") or f"HTTP {resp.status}")
        rejected = data.get("rejected") or []
        if any(key in rejected for key in values):
            raise KsApiError(f"device rejected settings: {rejected!r}")
        return data


async def run_command(
    session: aiohttp.ClientSession, host: str, token: str, command: str,
    *, pin: str | None,
) -> dict:
    """POST /api/commands/<command> with an empty body and a Bearer token
    (KSM-BEHAVE-082) -- checkUpdateNow, getUpdateStatus,
    getUpdateInstallerStatus and installUpdate all ride this same shape."""
    async with session.post(
        _credential_url(host, f"/api/commands/{command}", pin),
        data="{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        allow_redirects=False,
        ssl=_ssl(pin),
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        data = await resp.json()
        if not resp.ok:
            raise KsApiError(data.get("error") or f"HTTP {resp.status}")
        return data


async def check_ha_connection(
    session: aiohttp.ClientSession, host: str, token: str, *, pin: str | None
) -> bool:
    async with session.post(
        _credential_url(host, "/api/commands/haCheckConnection", pin),
        data="{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        allow_redirects=False,
        ssl=_ssl(pin),
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        data = await resp.json()
        return bool(data.get("ok"))
