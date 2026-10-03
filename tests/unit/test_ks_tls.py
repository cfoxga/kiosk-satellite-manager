"""Pinned HTTPS management transport (KSM-BEHAVE-093/094, #57).

The pinning tests run aiohttp against a real loopback TLS listener serving a
freshly generated self-signed certificate, so the pin check is the one aiohttp
actually performs at connection time -- not a mocked call.
"""
from __future__ import annotations

import asyncio
import datetime
import ssl
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from custom_components.kiosk_satellite_manager import install, ks_api_client, ks_tls, provisioning
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError


def _cert(key, serial: int) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kiosk-satellite")])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _server_context(tmp_path, key, cert, tag: str) -> ssl.SSLContext:
    cert_file = tmp_path / f"{tag}.crt"
    key_file = tmp_path / f"{tag}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context


class _Listener:
    """Loopback HTTPS endpoint recording every request byte it receives."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.server: asyncio.AbstractServer | None = None

    async def _handle(self, reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=2)
        except (ssl.SSLError, ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            data = b""
        if data:
            self.received.append(data)
            body = b'{"token":"device-token","ok":true,"rejected":[],"appVersion":"2026.9.84"}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            try:
                await writer.drain()
            except ConnectionError:
                pass
        writer.close()

    async def start(self, context: ssl.SSLContext | None) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=context)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()


@pytest.fixture
def device_key():
    return ec.generate_private_key(ec.SECP256R1())


async def test_matching_spki_pin_reaches_device_and_survives_same_key_renewal(
    tmp_path, monkeypatch, device_key
):
    """[KSM-TEST-177] The pinned key is accepted, including after KS reissues
    its certificate on the same key (automatic renewal)."""
    original = _cert(device_key, 1)
    renewed = _cert(device_key, 2)
    pin = ks_api_client.spki_sha256_from_pem(_pem(original))
    assert pin == ks_api_client.spki_sha256_from_pem(_pem(renewed))

    for tag, cert in (("original", original), ("renewed", renewed)):
        listener = _Listener()
        port = await listener.start(_server_context(tmp_path, device_key, cert, tag))
        monkeypatch.setattr(ks_api_client, "HEALTH_PORT", port)
        try:
            async with aiohttp.ClientSession() as session:
                token = await ks_api_client.login(session, "127.0.0.1", "synthetic-pw", pin=pin)
            assert token == "device-token"
            assert len(listener.received) == 1
            assert b"synthetic-pw" in listener.received[0]
        finally:
            await listener.stop()


@pytest.mark.parametrize(
    "operation",
    [
        lambda s, h, pin: ks_api_client.login(s, h, "synthetic-pw", pin=pin),
        lambda s, h, pin: ks_api_client.setup_password(s, h, "synthetic-pw", "Kitchen", pin=pin),
        lambda s, h, pin: ks_api_client.patch_settings(
            s, h, "synthetic-device-token", {"ha.token": "synthetic-ha-token"}, pin=pin
        ),
        lambda s, h, pin: ks_api_client.run_command(s, h, "synthetic-device-token", "x", pin=pin),
        lambda s, h, pin: ks_api_client.get_health(s, h, pin=pin),
    ],
)
async def test_wrong_key_is_refused_before_any_request_byte(
    tmp_path, monkeypatch, device_key, operation
):
    """[KSM-TEST-178] A different key fails the pin at connection time; the
    listener receives nothing, so no credential left the client."""
    attacker_key = ec.generate_private_key(ec.SECP256R1())
    pin = ks_api_client.spki_sha256_from_pem(_pem(_cert(device_key, 1)))
    listener = _Listener()
    port = await listener.start(_server_context(tmp_path, attacker_key, _cert(attacker_key, 9), "mitm"))
    monkeypatch.setattr(ks_api_client, "HEALTH_PORT", port)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ServerFingerprintMismatch):
                await operation(session, "127.0.0.1", pin)
        await asyncio.sleep(0.1)
        assert listener.received == []
    finally:
        await listener.stop()


async def test_probe_reports_the_served_key_and_none_for_plain_http(
    tmp_path, monkeypatch, device_key
):
    """[KSM-TEST-177] The trust-on-first-use probe returns the served SPKI over
    TLS, and None when the device answers only plain HTTP."""
    cert = _cert(device_key, 1)
    tls_listener = _Listener()
    port = await tls_listener.start(_server_context(tmp_path, device_key, cert, "probe"))
    monkeypatch.setattr(ks_api_client, "HEALTH_PORT", port)
    try:
        async with aiohttp.ClientSession() as session:
            served = await ks_api_client.probe_https(session, "127.0.0.1")
        assert served is not None
        assert served[0] == ks_api_client.spki_sha256_from_pem(_pem(cert))
    finally:
        await tls_listener.stop()

    plain = _Listener()
    port = await plain.start(None)
    monkeypatch.setattr(ks_api_client, "HEALTH_PORT", port)
    try:
        async with aiohttp.ClientSession() as session:
            assert await ks_api_client.probe_https(session, "127.0.0.1") is None
    finally:
        await plain.stop()


PIN = "ab" * 32


@pytest.mark.parametrize(
    "call",
    [
        lambda s, pin: ks_api_client.get_setup_status(s, "10.0.0.5", pin=pin),
        lambda s, pin: ks_api_client.setup_password(s, "10.0.0.5", "pw", "Kitchen", pin=pin),
        lambda s, pin: ks_api_client.login(s, "10.0.0.5", "pw", pin=pin),
        lambda s, pin: ks_api_client.get_settings(s, "10.0.0.5", "tok", pin=pin),
        lambda s, pin: ks_api_client.patch_settings(s, "10.0.0.5", "tok", {"a": 1}, pin=pin),
        lambda s, pin: ks_api_client.run_command(s, "10.0.0.5", "tok", "cmd", pin=pin),
        lambda s, pin: ks_api_client.check_ha_connection(s, "10.0.0.5", "tok", pin=pin),
        lambda s, pin: ks_api_client.get_health(s, "10.0.0.5", pin=pin),
        lambda s, pin: provisioning.fetch_health(s, "10.0.0.5", pin=pin),
    ],
)
@pytest.mark.parametrize("pin, scheme", [(PIN, "https"), (None, "http")])
async def test_every_surface_follows_the_entry_pin_state(call, pin, scheme):
    """[KSM-TEST-179] Pinned -> https with the pin as the ssl check on every
    surface; unpinned -> http (KSM-BEHAVE-070 unchanged)."""
    resp = MagicMock(ok=True, status=200)
    resp.json = AsyncMock(return_value={"token": "t", "ok": True, "rejected": []})
    resp.raise_for_status = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    for method in ("get", "post", "patch"):
        setattr(session, method, MagicMock(return_value=cm))

    await call(session, pin)

    used = [m for m in (session.get, session.post, session.patch) if m.called]
    assert len(used) == 1
    url = used[0].call_args.args[0]
    assert url.startswith(f"{scheme}://10.0.0.5:2324/")
    ssl_arg = used[0].call_args.kwargs.get("ssl")
    if pin:
        assert isinstance(ssl_arg, ks_api_client.SpkiPin)
        assert ssl_arg.fingerprint == bytes.fromhex(PIN)


def test_credential_url_refuses_the_scheme_the_pin_state_does_not_imply(monkeypatch):
    """[KSM-TEST-179] A pinned entry cannot be steered back to http, nor an
    unpinned one to https."""
    monkeypatch.setattr(ks_api_client, "_base_url", lambda host, pin: f"http://{host}:2324")
    with pytest.raises(KsApiError):
        ks_api_client._credential_url("10.0.0.5", "/api/login", PIN)
    monkeypatch.setattr(ks_api_client, "_base_url", lambda host, pin: f"https://{host}:2324")
    with pytest.raises(KsApiError):
        ks_api_client._credential_url("10.0.0.5", "/api/login", None)


def _fake_api(monkeypatch, *, probes, version="2026.9.84", certificate_pem=None):
    """Script ks_api_client for async_establish_tls, recording each call."""
    calls: list[tuple] = []
    probe_results = list(probes)

    async def probe_https(session, host, path="/api/health"):
        calls.append(("probe",))
        return probe_results.pop(0) if probe_results else None

    async def get_health(session, host, *, pin):
        calls.append(("health", pin))
        return {"appVersion": version}

    async def login(session, host, password, *, pin):
        calls.append(("login", pin))
        return "tok"

    async def run_command(session, host, token, command, *, pin):
        calls.append(("command", command, pin))
        return {"ok": True, "data": {"certificate": certificate_pem}}

    async def patch_settings(session, host, token, values, *, pin):
        calls.append(("patch", values, pin))
        return {"ok": True}

    for name, fn in {
        "probe_https": probe_https,
        "get_health": get_health,
        "login": login,
        "run_command": run_command,
        "patch_settings": patch_settings,
    }.items():
        monkeypatch.setattr(ks_tls.ks_api_client, name, fn)
    monkeypatch.setattr(ks_tls, "TLS_ENABLE_POLL_DELAY_S", 0)
    return calls


async def test_establish_enables_https_and_pins_the_key_it_was_told_about(monkeypatch, device_key):
    """[KSM-TEST-180] Enable over HTTP, then pin only the served key matching
    the pre-switch tlsCertificate."""
    pem = _pem(_cert(device_key, 1))
    spki = ks_api_client.spki_sha256_from_pem(pem)
    calls = _fake_api(monkeypatch, probes=[None, None, (spki, {})], certificate_pem=pem)

    assert await ks_tls.async_establish_tls(MagicMock(), "10.0.0.5", "pw") == spki
    patches = [c for c in calls if c[0] == "patch"]
    assert patches == [("patch", {"remote.tls": True}, None)]
    assert ("command", "tlsCertificate", None) in calls
    assert calls[-1] == ("probe",)


async def test_establish_refuses_a_served_key_that_differs_from_the_reported_one(
    monkeypatch, device_key
):
    """[KSM-TEST-181] A different key after the switch pins nothing."""
    pem = _pem(_cert(device_key, 1))
    calls = _fake_api(monkeypatch, probes=[None, ("cd" * 32, {})], certificate_pem=pem)
    with pytest.raises(KsApiError):
        await ks_tls.async_establish_tls(MagicMock(), "10.0.0.5", "pw")


async def test_establish_times_out_when_https_never_comes_up(monkeypatch, device_key):
    """[KSM-TEST-180] No HTTPS answer within the window raises, pins nothing."""
    pem = _pem(_cert(device_key, 1))
    _fake_api(monkeypatch, probes=[], certificate_pem=pem)
    with pytest.raises(KsApiError):
        await ks_tls.async_establish_tls(MagicMock(), "10.0.0.5", "pw")


@pytest.mark.parametrize("version", ["2026.9.77", "2025.12.200", "", None, "dev-build"])
async def test_establish_leaves_old_or_unknown_versions_on_http(monkeypatch, version):
    """[KSM-TEST-181] KS without TLS support: no login, no remote.tls PATCH."""
    calls = _fake_api(monkeypatch, probes=[None], version=version)
    assert await ks_tls.async_establish_tls(MagicMock(), "10.0.0.5", "pw") is None
    assert [c[0] for c in calls] == ["probe", "health"]


async def test_establish_trusts_an_already_https_device_without_http_login(monkeypatch):
    """[KSM-TEST-181] A device already serving HTTPS is pinned on first use."""
    calls = _fake_api(monkeypatch, probes=[("ef" * 32, {})])
    assert await ks_tls.async_establish_tls(MagicMock(), "10.0.0.5", "pw") == "ef" * 32
    assert calls == [("probe",)]


def _sync_fakes(monkeypatch, *, establish):
    events: list[tuple] = []

    async def wait_status(session, host):
        events.append(("status",))
        return {"passwordNeeded": False, "deviceName": "Old"}, None

    async def login(session, host, password, *, pin):
        events.append(("login", pin))
        return f"tok-{pin}"

    async def patch_settings(session, host, token, values, *, pin):
        events.append(("patch", tuple(sorted(values)), pin, token))
        return {"ok": True}

    async def check(session, host, token, *, pin):
        events.append(("check", pin))
        return True, None

    async def fake_establish(session, host, password):
        events.append(("establish",))
        return await establish()

    monkeypatch.setattr(install, "_wait_for_setup_status", wait_status)
    monkeypatch.setattr(install.ks_api_client, "login", login)
    monkeypatch.setattr(install.ks_api_client, "patch_settings", patch_settings)
    monkeypatch.setattr(install.ks_api_client, "check_ha_connection", check)
    monkeypatch.setattr(install.ks_tls, "async_establish_tls", fake_establish)
    return events


async def _run_sync(pinned: list, before_ha_setup=None, establish_tls=True):
    """`establish_tls=True` is Install on a pinned entry (the operator opted
    in earlier, KSM-BEHAVE-094); False is onboarding, which keeps the device's
    transport (#137)."""
    from custom_components.kiosk_satellite_manager.credentials import TokenCredential

    return await install._sync_device_and_connect_ha(
        MagicMock(),
        MagicMock(),
        "10.0.0.5",
        "Kitchen",
        "pw",
        recipe=MagicMock(start_url_path="/start"),
        token_credential=TokenCredential("synthetic-ha-token", None, owned=False),
        ha_url="https://ha.example",
        on_tls_pinned=pinned.append,
        before_ha_setup=before_ha_setup,
        establish_tls=establish_tls,
    )


async def test_onboarding_sends_the_ha_token_only_over_the_pinned_channel(monkeypatch):
    """[KSM-TEST-180] Install on a pinned entry: ha.token goes out only after
    TLS is pinned, on a pinned call authenticated by a login made over that
    pinned channel."""

    async def establish():
        return PIN

    events = _sync_fakes(monkeypatch, establish=establish)
    pinned: list = []
    await _run_sync(pinned)

    assert pinned == [PIN]
    ha_patch = next(e for e in events if e[0] == "patch" and "ha.token" in e[1])
    assert ha_patch[2] == PIN
    assert ha_patch[3] == f"tok-{PIN}"
    assert events.index(("establish",)) < events.index(ha_patch)
    assert all(e[2] == PIN for e in events if e[0] == "patch")
    assert ("check", PIN) in events


async def test_fleet_invitation_runs_after_admin_bootstrap_before_ha_configuration(monkeypatch):
    """[KSM-TEST-247] A selected Fleet can configure the device before HA setup."""
    async def establish():
        return PIN

    events = _sync_fakes(monkeypatch, establish=establish)

    async def invite():
        events.append(("invite",))

    await _run_sync([], before_ha_setup=invite)
    assert events.index(("login", PIN)) < events.index(("invite",))
    ha_patch = next(e for e in events if e[0] == "patch" and "ha.token" in e[1])
    assert events.index(("invite",)) < events.index(ha_patch)


async def test_onboarding_sends_no_ha_token_when_tls_cannot_be_established(monkeypatch):
    """[KSM-TEST-180] Negative: a TLS-capable device whose switch fails gets
    no HA credential at all, and the failure propagates."""

    async def establish():
        raise KsApiError("HTTPS never came up")

    events = _sync_fakes(monkeypatch, establish=establish)
    pinned: list = []
    with pytest.raises(KsApiError):
        await _run_sync(pinned)
    assert pinned == []
    assert not [e for e in events if e[0] == "patch" and "ha.token" in e[1]]


async def test_onboarding_old_ks_keeps_the_http_path(monkeypatch):
    """[KSM-TEST-181] establish -> None keeps KSM-BEHAVE-070's HTTP sync."""

    async def establish():
        return None

    events = _sync_fakes(monkeypatch, establish=establish)
    pinned: list = []
    await _run_sync(pinned)
    assert pinned == []
    ha_patch = next(e for e in events if e[0] == "patch" and "ha.token" in e[1])
    assert ha_patch[2] is None


async def test_establish_refuses_when_tls_certificate_is_not_returned(monkeypatch):
    """[KSM-TEST-181] A refused tlsCertificate command enables nothing."""
    calls = _fake_api(monkeypatch, probes=[None], certificate_pem=None)
    with pytest.raises(KsApiError):
        await ks_tls.async_establish_tls(MagicMock(), "10.0.0.5", "pw")
    assert not [c for c in calls if c[0] == "patch"]


async def test_onboarding_refuses_a_key_that_changes_mid_onboarding(monkeypatch):
    """[KSM-TEST-180] Setup status served over HTTPS with one key and a pin
    for another: nothing pinned, no HA credential sent."""

    async def establish():
        return PIN

    events = _sync_fakes(monkeypatch, establish=establish)

    async def wait_status(session, host):
        return {"passwordNeeded": False, "deviceName": "Kitchen"}, "cd" * 32

    monkeypatch.setattr(install, "_wait_for_setup_status", wait_status)
    pinned: list = []
    with pytest.raises(KsApiError):
        await _run_sync(pinned)
    assert pinned == []
    assert not [e for e in events if e[0] in ("patch", "login")]


async def _never_establish():
    raise AssertionError("onboarding must not switch the device to HTTPS (#137)")


async def test_onboarding_keeps_an_http_device_on_http(monkeypatch):
    """[KSM-TEST-335] Onboarding a TLS-capable device that serves HTTP never
    calls async_establish_tls, sends no remote.tls, pins nothing and writes
    the HA settings over HTTP."""
    events = _sync_fakes(monkeypatch, establish=_never_establish)
    pinned: list = []
    await _run_sync(pinned, establish_tls=False)

    assert ("establish",) not in events
    assert pinned == []
    patches = [e for e in events if e[0] == "patch"]
    assert patches and all(e[2] is None for e in patches)
    assert not [e for e in patches if "remote.tls" in e[1]]
    ha_patch = next(e for e in patches if "ha.token" in e[1])
    assert ha_patch[3] == "tok-None"


async def test_onboarding_pins_a_device_that_already_serves_https(monkeypatch):
    """[KSM-TEST-335] A device already on HTTPS is pinned to the key it
    serves (trust on first use) without being switched, and every
    authenticated call runs over that pinned channel."""
    events = _sync_fakes(monkeypatch, establish=_never_establish)

    async def wait_status(session, host):
        events.append(("status",))
        return {"passwordNeeded": False, "deviceName": "Old"}, PIN

    monkeypatch.setattr(install, "_wait_for_setup_status", wait_status)
    pinned: list = []
    await _run_sync(pinned, establish_tls=False)

    assert ("establish",) not in events
    assert pinned == [PIN]
    assert [e for e in events if e[0] == "login"] == [("login", PIN)]
    patches = [e for e in events if e[0] == "patch"]
    assert patches and all(e[2] == PIN for e in patches)
    assert not [e for e in patches if "remote.tls" in e[1]]


async def test_unauthenticated_reads_prefer_the_https_answer(monkeypatch):
    """[KSM-TEST-179] Setup status and install health read over HTTPS when
    the device serves it, with no HTTP request at all."""

    async def probe_https(session, host, path="/api/health"):
        return ("ef" * 32, {"path": path})

    async def no_http(*args, **kwargs):
        raise AssertionError("HTTP read on an HTTPS device")

    monkeypatch.setattr(install.ks_api_client, "probe_https", probe_https)
    monkeypatch.setattr(install.ks_api_client, "get_setup_status", no_http)
    monkeypatch.setattr(install.ks_api_client, "get_health", no_http)

    assert await install._wait_for_setup_status(MagicMock(), "10.0.0.5") == (
        {"path": "/api/setup/status"},
        "ef" * 32,
    )
    assert await install._read_health_any(MagicMock(), "10.0.0.5") == {"path": "/api/health"}


@pytest.mark.parametrize("http_answers", [True, False])
async def test_disable_switches_off_over_the_pin_and_waits_for_http(monkeypatch, http_answers):
    """[KSM-TEST-337] Switch back to HTTP: login and remote.tls=false travel
    over the pinned channel; success needs an HTTP health answer, and with
    none the call raises so the caller keeps the pin."""
    calls = _fake_api(monkeypatch, probes=[])
    if not http_answers:
        async def get_health(session, host, *, pin):
            calls.append(("health", pin))
            raise aiohttp.ClientConnectionError("still on https")

        monkeypatch.setattr(ks_tls.ks_api_client, "get_health", get_health)
    if http_answers:
        await ks_tls.async_disable_tls(MagicMock(), "10.0.0.5", "pw", PIN)
    else:
        with pytest.raises(KsApiError):
            await ks_tls.async_disable_tls(MagicMock(), "10.0.0.5", "pw", PIN)
    assert calls[:2] == [("login", PIN), ("patch", {"remote.tls": False}, PIN)]
    health = [c for c in calls if c[0] == "health"]
    assert health and all(c == ("health", None) for c in health)
    assert len(health) == (1 if http_answers else ks_tls.TLS_ENABLE_POLL_ATTEMPTS)
