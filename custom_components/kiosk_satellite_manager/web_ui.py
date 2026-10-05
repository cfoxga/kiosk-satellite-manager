"""Isolated, device-scoped proxy for Kiosk Satellite's own Web UI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import secrets
import time
from urllib.parse import parse_qs, quote

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.http import request_handler_factory

from . import fleet
from .const import CONF_HOST, CONF_PASSWORD, CONF_TLS_SPKI, DOMAIN, MANAGER_ENTRY_KEY
from .ks_api_client import KsApiError, _credential_url, _ssl, login

_KEY = f"{DOMAIN}_web_ui"
_TTL = 60 * 60
_TIMEOUT = aiohttp.ClientTimeout(total=60)
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Access-Control-Allow-Origin": "null",
    "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "authorization, content-type",
    "Vary": "Origin",
}
_DOCUMENT_HEADERS = {
    **_HEADERS,
    "Content-Security-Policy": (
        "sandbox allow-scripts allow-forms allow-downloads; "
        "default-src 'self' data: blob:; connect-src 'self' ws: wss:; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; frame-ancestors 'self'"
    ),
}


@dataclass
class Grant:
    user_id: str
    entry_id: str
    subentry_id: str
    expires: float
    device_token: str | None = None


def _grants(hass) -> dict[str, Grant]:
    return hass.data.setdefault(_KEY, {})


def _device(hass, entry_id: str, subentry_id: str):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        return None
    subentry = entry.subentries.get(subentry_id)
    if subentry is None or subentry.subentry_type != "device":
        return None
    return fleet.DeviceEntry(hass, entry, subentry)


def issue_grant(hass, user, entry_id: str, subentry_id: str) -> str | None:
    """Issue a random URL only to an admin for a loaded managed device."""
    if not user.is_admin or not hass.data.get(MANAGER_ENTRY_KEY):
        return None
    device = _device(hass, entry_id, subentry_id)
    if device is None or not device.data.get(CONF_HOST) or not device.data.get(CONF_PASSWORD):
        return None
    grants = _grants(hass)
    now = time.monotonic()
    for key, grant in tuple(grants.items()):
        if grant.expires <= now:
            grants.pop(key, None)
    key = secrets.token_urlsafe(32)
    grants[key] = Grant(user.id, entry_id, subentry_id, now + _TTL)
    return f"/api/{DOMAIN}/web/{key}/"


async def _resolve(hass, key: str):
    grant = _grants(hass).get(key)
    if grant is None or grant.expires <= time.monotonic() or not hass.data.get(MANAGER_ENTRY_KEY):
        raise web.HTTPForbidden()
    user = await hass.auth.async_get_user(grant.user_id)
    if user is None or not user.is_active or not user.is_admin:
        raise web.HTTPForbidden()
    device = _device(hass, grant.entry_id, grant.subentry_id)
    if device is None or not device.data.get(CONF_HOST):
        raise web.HTTPForbidden()
    return device


def _path(tail: str, query: str) -> str:
    """Keep requests relative to the kiosk's root, never an arbitrary URL."""
    segments = tail.split("/")
    if any(part in (".", "..") or "\\" in part or "\x00" in part for part in segments):
        raise web.HTTPBadRequest()
    path = "/" + "/".join(quote(part, safe="-._~") for part in segments)
    return path + (f"?{query}" if query else "")


def _bootstrap(html: bytes, token: str) -> bytes:
    """Make device storage private to this sandboxed frame and prefill login."""
    safe = json.dumps(token).replace("<", "\\u003c")
    script = (
        "<script>const ksmStore=new Map([['ks_token'," + safe + "]]);"
        "Object.defineProperty(window,'localStorage',{configurable:true,value:{"
        "getItem:k=>ksmStore.has(k)?ksmStore.get(k):null,"
        "setItem:(k,v)=>ksmStore.set(k,String(v)),"
        "removeItem:k=>ksmStore.delete(k),clear:()=>ksmStore.clear()}});</script>"
    )
    return html.replace(b"<head>", b"<head>" + script.encode(), 1)


class KsmWebUiView(HomeAssistantView):
    """Capability-scoped reverse proxy; HA auth headers never reach the kiosk."""

    url = f"/api/{DOMAIN}/web/{{grant}}/{{tail:.*}}"
    name = f"api:{DOMAIN}:web_ui"
    requires_auth = False
    cors_allowed = False

    def __init__(self, hass):
        self.hass = hass

    def register(self, hass, app, router):
        """Register our own preflight handler for the frame's opaque origin."""
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            handler = request_handler_factory(hass, self, getattr(self, method.lower()))
            router.add_route(method, self.url, handler)

    async def options(self, request, grant, tail):
        await _resolve(self.hass, grant)
        if request.headers.get("Origin") != "null":
            raise web.HTTPForbidden()
        return web.Response(status=204, headers=_HEADERS)

    async def get(self, request, grant, tail):
        return await self._handle(request, grant, tail)

    async def head(self, request, grant, tail):
        return await self._handle(request, grant, tail)

    async def post(self, request, grant, tail):
        return await self._handle(request, grant, tail)

    async def put(self, request, grant, tail):
        return await self._handle(request, grant, tail)

    async def patch(self, request, grant, tail):
        return await self._handle(request, grant, tail)

    async def delete(self, request, grant, tail):
        return await self._handle(request, grant, tail)

    async def _handle(self, request, grant, tail):
        device = await _resolve(self.hass, grant)
        if request.headers.get("Origin") not in (None, "null"):
            raise web.HTTPForbidden()
        path = _path(tail, request.query_string)
        host = device.data[CONF_HOST]
        pin = device.data.get(CONF_TLS_SPKI)
        session = async_get_clientsession(self.hass)
        issued = _grants(self.hass)[grant]
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if tail != "api/ws":
                raise web.HTTPBadRequest()
            if not issued.device_token or parse_qs(request.query_string).get("token") != [issued.device_token]:
                raise web.HTTPForbidden()
            return await self._websocket(request, session, host, pin, path, grant)
        if not tail and request.method == "GET":
            password = device.data.get(CONF_PASSWORD)
            if not password:
                raise web.HTTPServiceUnavailable(text="Device password is missing")
            try:
                token = await login(session, host, password, pin=pin)
            except (aiohttp.ClientError, KsApiError, TimeoutError, ValueError, KeyError) as err:
                raise web.HTTPBadGateway(text="Device sign-in failed") from err
            issued.device_token = token
        else:
            token = None
        # The target is constructed from the saved device host and fixed management port.
        target = _credential_url(host, path, pin)
        headers = {}
        for name in ("Content-Type", "Accept", "Range"):
            if name in request.headers:
                headers[name] = request.headers[name]
        authorization = request.headers.get("Authorization")
        if authorization:
            if not issued.device_token or authorization != f"Bearer {issued.device_token}":
                raise web.HTTPForbidden()
            headers["Authorization"] = authorization
        try:
            async with session.request(
                request.method, target, data=request.content if request.can_read_body else None,
                headers=headers, ssl=_ssl(pin), allow_redirects=False, timeout=_TIMEOUT,
            ) as upstream:
                if 300 <= upstream.status < 400:
                    raise web.HTTPBadGateway(text="Device redirect refused")
                content_type = upstream.headers.get("Content-Type", "application/octet-stream")
                if token is not None:
                    body = await upstream.read()
                    if upstream.status != 200 or b"<head>" not in body[:4096]:
                        raise web.HTTPBadGateway(text="Device Web UI unavailable")
                    return web.Response(
                        body=_bootstrap(body, token), status=200,
                        headers={**_DOCUMENT_HEADERS, "Content-Type": content_type},
                    )
                response = web.StreamResponse(
                    status=upstream.status,
                    headers={**_HEADERS, "Content-Type": content_type, **({
                        "Content-Disposition": upstream.headers["Content-Disposition"]
                    } if "Content-Disposition" in upstream.headers else {})},
                )
                await response.prepare(request)
                if request.method != "HEAD":
                    async for chunk in upstream.content.iter_chunked(64 * 1024):
                        await response.write(chunk)
                await response.write_eof()
                return response
        except (aiohttp.ClientError, TimeoutError) as err:
            raise web.HTTPBadGateway(text="Device Web UI unavailable") from err

    async def _websocket(self, request, session, host, pin, path, grant):
        target = _credential_url(host, path, pin).replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        try:
            upstream = await session.ws_connect(target, ssl=_ssl(pin), timeout=15)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise web.HTTPBadGateway(text="Device WebSocket unavailable") from err
        client = web.WebSocketResponse()
        await client.prepare(request)

        async def to_device():
            async for message in client:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await upstream.send_str(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await upstream.send_bytes(message.data)

        async def to_browser():
            async for message in upstream:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await client.send_str(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await client.send_bytes(message.data)

        async def authorize_while_open():
            while not client.closed and not upstream.closed:
                await asyncio.sleep(5)
                try:
                    await _resolve(self.hass, grant)
                except web.HTTPException:
                    await upstream.close()
                    return True

        guard = asyncio.create_task(authorize_while_open())
        tasks = (asyncio.create_task(to_device()), asyncio.create_task(to_browser()), guard)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        revoked = guard in done and guard.result() is True
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.close()
        await client.close(code=1008 if revoked else 1000)
        return client


def async_register(hass):
    if not hass.data.get(f"{_KEY}_registered"):
        hass.http.register_view(KsmWebUiView(hass))
        hass.data[f"{_KEY}_registered"] = True


def async_unload(hass):
    _grants(hass).clear()
