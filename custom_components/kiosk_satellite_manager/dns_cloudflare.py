"""Cloudflare DNS for the ACME DNS-01 challenge (KSM-BEHAVE-203/204, #200).

Only the REST calls KSM needs: verify a token, list its zones, and add or
remove one TXT record. The token travels only in the Authorization header;
errors carry a fixed code, never a response body or the token.
"""
from __future__ import annotations

from typing import NamedTuple

import aiohttp

API = "https://api.cloudflare.com/client/v4"
_TIMEOUT = aiohttp.ClientTimeout(total=30)
_MAX_PAGES = 20


class CloudflareError(Exception):
    """A fixed failure code: invalid_token, cannot_connect or api_error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Zone(NamedTuple):
    id: str
    name: str
    name_servers: tuple[str, ...]


async def _call(
    session: aiohttp.ClientSession, token: str, method: str, path: str, **kwargs
) -> dict:
    try:
        async with session.request(
            method, f"{API}{path}", headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT, **kwargs,
        ) as resp:
            status = resp.status
            body = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        raise CloudflareError("cannot_connect") from None
    if status in (401, 403):
        raise CloudflareError("invalid_token")
    if not isinstance(body, dict) or not body.get("success"):
        raise CloudflareError("api_error")
    return body


async def async_verify_token(session: aiohttp.ClientSession, token: str) -> None:
    """Raise CloudflareError("invalid_token") unless the token is active."""
    if not token:
        raise CloudflareError("invalid_token")
    try:
        body = await _call(session, token, "GET", "/user/tokens/verify")
    except CloudflareError as err:
        if err.code == "api_error":
            raise CloudflareError("invalid_token") from None
        raise
    if (body.get("result") or {}).get("status") != "active":
        raise CloudflareError("invalid_token")


async def async_zones(session: aiohttp.ClientSession, token: str) -> list[Zone]:
    """Every zone the token can read."""
    zones: list[Zone] = []
    for page in range(1, _MAX_PAGES + 1):
        body = await _call(session, token, "GET", "/zones",
                           params={"per_page": "50", "page": str(page)})
        for item in body.get("result") or []:
            if isinstance(item, dict) and item.get("id") and item.get("name"):
                zones.append(Zone(
                    str(item["id"]), str(item["name"]).lower().rstrip("."),
                    tuple(str(ns) for ns in item.get("name_servers") or ()),
                ))
        if page >= int((body.get("result_info") or {}).get("total_pages") or 1):
            break
    return zones


def find_zone(zones: list[Zone], hostname: str) -> Zone | None:
    """The zone whose name is the longest suffix of `hostname`."""
    hostname = hostname.lower().rstrip(".")
    matches = [z for z in zones if hostname == z.name or hostname.endswith("." + z.name)]
    return max(matches, key=lambda z: len(z.name), default=None)


async def async_create_txt(
    session: aiohttp.ClientSession, token: str, zone_id: str, name: str, content: str
) -> str:
    body = await _call(session, token, "POST", f"/zones/{zone_id}/dns_records", json={
        "type": "TXT", "name": name, "content": content, "ttl": 60,
    })
    record_id = (body.get("result") or {}).get("id")
    if not record_id:
        raise CloudflareError("api_error")
    return str(record_id)


async def async_delete_txt(
    session: aiohttp.ClientSession, token: str, zone_id: str, record_id: str
) -> None:
    await _call(session, token, "DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
