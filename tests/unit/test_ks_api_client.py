"""Unit tests for the Kiosk Satellite on-device web UI client
(KSM-BEHAVE-010/011). Endpoint shapes were extracted live from the Test
Portal's own served JS bundles -- see ks_api_client.py's module docstring.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.kiosk_satellite_manager import ks_api_client
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError


def _fake_response(json_data: dict, ok: bool = True, status: int = 200):
    resp = MagicMock()
    resp.ok = ok
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.raise_for_status = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_session(**method_returns):
    session = MagicMock()
    for method, return_value in method_returns.items():
        setattr(session, method, MagicMock(return_value=return_value))
    return session


async def test_get_setup_status_gets_unauthenticated_status_url():
    resp_cm = _fake_response({"setupNeeded": True, "passwordNeeded": True})
    session = _fake_session(get=resp_cm)
    result = await ks_api_client.get_setup_status(session, "192.168.1.50", pin=None)
    assert result == {"setupNeeded": True, "passwordNeeded": True}
    session.get.assert_called_once()
    assert session.get.call_args.args[0] == "http://192.168.1.50:2324/api/setup/status"


async def test_get_setup_status_bounds_a_malformed_http_response(monkeypatch):
    """[KSM-TEST-108] Exercise aiohttp's real parser against a malformed reply.

    The timeout bounds this regression so an invalid device response cannot turn
    into a hung KSM operation; the invalid NUL in a header value must be
    rejected by the client parser rather than accepted as JSON.
    """

    async def malformed_server(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\x00\r\n\r\n{}")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(malformed_server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(ks_api_client, "_base_url", lambda _host, _pin: f"http://127.0.0.1:{port}")
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientError):
                await asyncio.wait_for(
                    ks_api_client.get_setup_status(session, "malformed-device", pin=None), timeout=1
                )
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "operation, expected_secret",
    [
        (
            lambda session, host: ks_api_client.setup_password(
                session, host, "synthetic-password", "Kitchen", pin=None,
            ),
            b"synthetic-password",
        ),
        (lambda session, host: ks_api_client.login(session, host, "synthetic-password", pin=None), b"synthetic-password"),
        (
            lambda session, host: ks_api_client.patch_settings(
                session, host, "synthetic-device-token", {"ha.token": "synthetic-ha-token"}, pin=None,
            ),
            b"synthetic-ha-token",
        ),
        (
            lambda session, host: ks_api_client.check_ha_connection(
                session, host, "synthetic-device-token", pin=None,
            ),
            b"synthetic-device-token",
        ),
        (
            lambda session, host: ks_api_client.get_settings(session, host, "synthetic-device-token", pin=None),
            b"synthetic-device-token",
        ),
    ],
)
async def test_credential_operations_reach_configured_http_device(monkeypatch, operation, expected_secret):
    """[KSM-TEST-126] KS HTTP compatibility sends each operation to its configured host."""
    requests_received = asyncio.Event()
    captured = []

    async def capture_server(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in headers.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        body = await reader.readexactly(length) if length else b""
        captured.append((headers, body))
        requests_received.set()
        body = b'{"token":"device-token","rejected":[],"ok":true}'
        writer.write(
            b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(capture_server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(ks_api_client, "HEALTH_PORT", port)
    try:
        async with aiohttp.ClientSession() as session:
            await operation(session, "127.0.0.1")
        await asyncio.wait_for(requests_received.wait(), timeout=1)
        assert len(captured) == 1
        assert b"Host: 127.0.0.1:" in captured[0][0]
        assert expected_secret in captured[0][0] + captured[0][1]
    finally:
        server.close()
        await server.wait_closed()


async def test_status_request_remains_available_to_http_capture(monkeypatch):
    """[KSM-TEST-126] Unauthenticated status remains reachable over HTTP."""
    received = asyncio.Event()

    async def status_server(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        assert b"GET /api/setup/status HTTP/1.1" in request
        received.set()
        body = b'{"setupNeeded":true,"passwordNeeded":true}'
        writer.write(
            b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(status_server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(ks_api_client, "_base_url", lambda _host, _pin: f"http://127.0.0.1:{port}")
    try:
        async with aiohttp.ClientSession() as session:
            assert await ks_api_client.get_setup_status(session, "status-device", pin=None) == {
                "setupNeeded": True,
                "passwordNeeded": True,
            }
        await asyncio.wait_for(received.wait(), timeout=0.1)
    finally:
        server.close()
        await server.wait_closed()


async def test_credential_operation_rejects_a_different_http_origin(monkeypatch):
    """[KSM-TEST-127] A base URL override cannot send a secret to another host."""
    monkeypatch.setattr(
        ks_api_client, "_base_url", lambda _host, _pin: "http://other-device.invalid:2324"
    )
    session = _fake_session(post=_fake_response({"token": "unused"}))

    with pytest.raises(KsApiError, match="configured device"):
        await ks_api_client.login(session, "configured-device.invalid", "synthetic-password", pin=None)

    session.post.assert_not_called()


async def test_credential_operation_rejects_a_different_port(monkeypatch):
    """[KSM-TEST-127] Host equality cannot hide a substituted port."""
    monkeypatch.setattr(ks_api_client, "_base_url", lambda _host, _pin: "http://127.0.0.1:8080")
    session = _fake_session(post=_fake_response({"token": "unused"}))

    with pytest.raises(KsApiError, match="configured device"):
        await ks_api_client.login(session, "127.0.0.1", "synthetic-password", pin=None)

    session.post.assert_not_called()


async def test_credential_operation_rejects_a_different_scheme(monkeypatch):
    """[KSM-TEST-127] The allowed origin includes the current HTTP scheme."""
    monkeypatch.setattr(ks_api_client, "_base_url", lambda _host, _pin: "https://127.0.0.1:2324")
    session = _fake_session(post=_fake_response({"token": "unused"}))

    with pytest.raises(KsApiError, match="configured device"):
        await ks_api_client.login(session, "127.0.0.1", "synthetic-password", pin=None)

    session.post.assert_not_called()


async def test_credential_redirect_does_not_forward_password(monkeypatch):
    """[KSM-TEST-128] A real 307 cannot deliver the password to another origin."""
    forwarded = asyncio.Event()

    async def destination(reader, writer):
        forwarded.set()
        writer.close()
        await writer.wait_closed()

    destination_server = await asyncio.start_server(destination, "127.0.0.1", 0)
    destination_port = destination_server.sockets[0].getsockname()[1]

    async def redirect(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        response = (
            "HTTP/1.1 307 Temporary Redirect\r\n"
            f"Location: http://127.0.0.1:{destination_port}/stolen\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        writer.write(response.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    redirect_server = await asyncio.start_server(redirect, "127.0.0.1", 0)
    monkeypatch.setattr(ks_api_client, "HEALTH_PORT", redirect_server.sockets[0].getsockname()[1])
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ContentTypeError):
                await ks_api_client.login(session, "127.0.0.1", "synthetic-password", pin=None)
        assert not forwarded.is_set()
    finally:
        redirect_server.close()
        destination_server.close()
        await redirect_server.wait_closed()
        await destination_server.wait_closed()


async def test_setup_password_posts_password_and_device_name_returns_token():
    session = _fake_session(post=_fake_response({"token": "tok-123"}))
    token = await ks_api_client.setup_password(session, "192.168.1.50", "hunter22", "Kitchen", pin=None)
    assert token == "tok-123"
    _, kwargs = session.post.call_args
    assert kwargs["json"] == {"password": "hunter22", "deviceName": "Kitchen"}
    assert kwargs["allow_redirects"] is False
    assert session.post.call_args.args[0] == "http://192.168.1.50:2324/api/setup/password"


async def test_setup_password_raises_ksapierror_on_rejection():
    session = _fake_session(post=_fake_response({"error": "already set"}, ok=False, status=403))
    with pytest.raises(KsApiError, match="already set"):
        await ks_api_client.setup_password(session, "192.168.1.50", "hunter22", "Kitchen", pin=None)


async def test_login_posts_password_returns_token():
    session = _fake_session(post=_fake_response({"token": "tok-456"}))
    token = await ks_api_client.login(session, "192.168.1.50", "hunter22", pin=None)
    assert token == "tok-456"
    _, kwargs = session.post.call_args
    assert kwargs["json"] == {"password": "hunter22"}
    assert kwargs["allow_redirects"] is False
    assert session.post.call_args.args[0] == "http://192.168.1.50:2324/api/login"


async def test_login_and_settings_rejections_fall_back_to_http_status():
    """[KSM-TEST-010] Empty device errors remain actionable and bounded."""
    login_session = _fake_session(post=_fake_response({}, ok=False, status=401))
    with pytest.raises(KsApiError, match="HTTP 401"):
        await ks_api_client.login(login_session, "192.168.1.50", "hunter22", pin=None)

    settings_session = _fake_session(patch=_fake_response({}, ok=False, status=503))
    with pytest.raises(KsApiError, match="HTTP 503"):
        await ks_api_client.patch_settings(
            settings_session, "192.168.1.50", "tok-789", {"ha.url": "https://ha.example"}, pin=None,
        )


async def test_patch_settings_sends_bearer_token_and_values():
    session = _fake_session(patch=_fake_response({"rejected": []}))
    await ks_api_client.patch_settings(
        session, "192.168.1.50", "tok-789", {"device.name": "Kitchen"}, pin=None,
    )
    _, kwargs = session.patch.call_args
    assert kwargs["json"] == {"device.name": "Kitchen"}
    assert kwargs["headers"]["Authorization"] == "Bearer tok-789"
    assert kwargs["allow_redirects"] is False
    assert session.patch.call_args.args[0] == "http://192.168.1.50:2324/api/settings"


async def test_patch_settings_raises_when_key_rejected():
    session = _fake_session(patch=_fake_response({"rejected": ["ha.url"]}))
    with pytest.raises(KsApiError, match="ha.url"):
        await ks_api_client.patch_settings(
            session, "192.168.1.50", "tok-789", {"ha.url": "not a url"}, pin=None,
        )


async def test_check_ha_connection_returns_ok_flag():
    session = _fake_session(post=_fake_response({"ok": True}))
    assert await ks_api_client.check_ha_connection(session, "192.168.1.50", "tok-789", pin=None) is True
    _, kwargs = session.post.call_args
    assert kwargs["allow_redirects"] is False
    assert session.post.call_args.args[0] == "http://192.168.1.50:2324/api/commands/haCheckConnection"


async def test_run_command_posts_bearer_token_to_the_command_url():
    """[KSM-BEHAVE-082] checkUpdateNow/getUpdateStatus/getUpdateInstallerStatus/
    installUpdate all ride this same POST /api/commands/<command> shape."""
    session = _fake_session(post=_fake_response({"availableVersion": "2026.9.77"}))
    result = await ks_api_client.run_command(session, "192.168.1.50", "tok-789", "getUpdateStatus", pin=None)
    assert result == {"availableVersion": "2026.9.77"}
    _, kwargs = session.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer tok-789"
    assert kwargs["allow_redirects"] is False
    assert session.post.call_args.args[0] == "http://192.168.1.50:2324/api/commands/getUpdateStatus"


async def test_run_command_raises_ksapierror_on_rejection():
    session = _fake_session(post=_fake_response({"error": "not authenticated"}, ok=False, status=401))
    with pytest.raises(KsApiError, match="not authenticated"):
        await ks_api_client.run_command(session, "192.168.1.50", "tok-789", "installUpdate", pin=None)


async def test_check_ha_connection_false_on_failure():
    session = _fake_session(post=_fake_response({"ok": False, "error": "unreachable"}))
    assert await ks_api_client.check_ha_connection(session, "192.168.1.50", "tok-789", pin=None) is False
    _, kwargs = session.post.call_args
    assert kwargs["allow_redirects"] is False
    assert session.post.call_args.args[0] == "http://192.168.1.50:2324/api/commands/haCheckConnection"


async def test_get_settings_maps_described_settings_to_values():
    """[KSM-TEST-175] GET /api/settings describes every setting; only the
    key -> current value map is returned."""
    session = _fake_session(
        get=_fake_response(
            {
                "settings": [
                    {"key": "device.name", "type": "string", "value": "Kitchen"},
                    {"key": "esphome.node_name", "type": "string", "value": "ks-kitchen"},
                    {"key": "no.value", "type": "string"},
                ],
                "subpageHints": {},
            }
        )
    )
    result = await ks_api_client.get_settings(session, "host", "device-token", pin=None)
    assert result == {"device.name": "Kitchen", "esphome.node_name": "ks-kitchen", "no.value": None}
    kwargs = session.get.call_args.kwargs
    assert kwargs["headers"] == {"Authorization": "Bearer device-token"}
    assert kwargs["allow_redirects"] is False


async def test_get_settings_raises_on_rejected_token():
    session = _fake_session(get=_fake_response({"error": "unauthorized"}, ok=False, status=401))
    with pytest.raises(KsApiError, match="unauthorized"):
        await ks_api_client.get_settings(session, "host", "bad-token", pin=None)


async def test_upload_update_posts_the_raw_apk_to_the_pinned_origin():
    """[KSM-TEST-210] KSM-BEHAVE-108: POST /api/update/upload on the entry's
    pinned origin, Bearer auth, explicit Content-Length, no redirects."""
    body = object()
    session = _fake_session(post=_fake_response({"ok": True, "data": {"buildNumber": 2}}))
    pin = "ab" * 32

    result = await ks_api_client.upload_update(session, "192.168.1.50", "tok", body, 1234, pin=pin)

    assert result == {"ok": True, "data": {"buildNumber": 2}}
    args, kwargs = session.post.call_args
    assert args[0] == "https://192.168.1.50:2324/api/update/upload"
    assert kwargs["data"] is body
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["headers"]["Content-Length"] == "1234"
    assert kwargs["allow_redirects"] is False
    assert isinstance(kwargs["ssl"], ks_api_client.SpkiPin)
    assert kwargs["timeout"].total > 60


async def test_upload_update_returns_a_refusal_envelope_for_the_caller():
    """[KSM-TEST-210] A 400 {ok:false, error} comes back as the envelope so
    the caller can report Kiosk Satellite's own text."""
    session = _fake_session(
        post=_fake_response({"ok": False, "error": "Downgrades are refused"}, ok=False, status=400)
    )
    result = await ks_api_client.upload_update(session, "192.168.1.50", "tok", b"", 1, pin=None)
    assert result == {"ok": False, "error": "Downgrades are refused"}


async def test_upload_update_raises_on_a_server_error():
    """[KSM-TEST-210] negative case: a 5xx (envelope or not) is an error, never
    a success the caller could mistake for an accepted upload."""
    session = _fake_session(post=_fake_response({"ok": False}, ok=False, status=500))
    with pytest.raises(KsApiError, match="HTTP 500"):
        await ks_api_client.upload_update(session, "192.168.1.50", "tok", b"", 1, pin=None)


async def test_export_config_gets_bearer_export_without_redirects():
    """[KSM-TEST-199] KSM-BEHAVE-104: KS's own whole-device export endpoint."""
    payload = {"kind": "kiosk-satellite-config", "settings": {"a": 1}}
    session = _fake_session(get=_fake_response(payload))
    assert await ks_api_client.export_config(session, "192.168.1.50", "tok", pin=None) == payload
    call = session.get.call_args
    assert call.args[0] == "http://192.168.1.50:2324/api/config/export"
    assert call.kwargs["headers"] == {"Authorization": "Bearer tok"}
    assert call.kwargs["allow_redirects"] is False


async def test_export_config_raises_on_error_status():
    """[KSM-TEST-199] A KS error reply is a KsApiError, never a payload."""
    session = _fake_session(get=_fake_response({"error": "denied"}, ok=False, status=401))
    with pytest.raises(KsApiError, match="denied"):
        await ks_api_client.export_config(session, "192.168.1.50", "tok", pin=None)


async def test_import_config_posts_replace_this_device_import():
    """[KSM-TEST-204] KSM-BEHAVE-106: adoptIdentity=1&importLocalStorage=1, JSON body."""
    payload = {"kind": "kiosk-satellite-config", "settings": {"a": 1}}
    session = _fake_session(post=_fake_response({"ok": True, "data": {"applied": 1}}))
    result = await ks_api_client.import_config(session, "192.168.1.50", "tok", payload, pin=None)
    assert result == {"ok": True, "data": {"applied": 1}}
    call = session.post.call_args
    assert call.args[0] == (
        "http://192.168.1.50:2324/api/config/import?adoptIdentity=1&importLocalStorage=1"
    )
    assert call.kwargs["json"] == payload
    assert call.kwargs["headers"] == {"Authorization": "Bearer tok"}
    assert call.kwargs["allow_redirects"] is False


async def test_import_config_raises_on_rejection():
    """[KSM-TEST-205] A non-OK import reply is a KsApiError carrying KS's reason."""
    session = _fake_session(post=_fake_response({"error": "bad file"}, ok=False, status=400))
    with pytest.raises(KsApiError, match="bad file"):
        await ks_api_client.import_config(session, "192.168.1.50", "tok", {}, pin=None)
