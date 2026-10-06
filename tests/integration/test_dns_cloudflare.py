"""[KSM-TEST-402/403/404] The Cloudflare REST client KSM-BEHAVE-203/204 use (#200)."""
from __future__ import annotations

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.kiosk_satellite_manager import dns_cloudflare as cf

API = cf.API
TOKEN = "cf-token-never-shown"


def _zone(n, name, ns=("a.ns.test",)):
    return {"id": f"z{n}", "name": name, "name_servers": list(ns)}


async def test_verify_token_accepts_only_an_active_token(hass, aioclient_mock):
    """[KSM-TEST-402] Active passes; inactive, a 401, an API refusal, a blank
    token and a transport error each raise their fixed code."""
    session = async_get_clientsession(hass)
    url = f"{API}/user/tokens/verify"
    aioclient_mock.get(url, json={"success": True, "result": {"status": "active"}})
    await cf.async_verify_token(session, TOKEN)
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == f"Bearer {TOKEN}"
    for kwargs, code in (
        ({"json": {"success": True, "result": {"status": "disabled"}}}, "invalid_token"),
        ({"status": 401, "json": {"success": False}}, "invalid_token"),
        ({"json": {"success": False}}, "invalid_token"),
        ({"exc": aiohttp.ClientError(f"boom {TOKEN}")}, "cannot_connect"),
        ({"exc": TimeoutError()}, "cannot_connect"),
    ):
        aioclient_mock.clear_requests()
        aioclient_mock.get(url, **kwargs)
        with pytest.raises(cf.CloudflareError) as raised:
            await cf.async_verify_token(session, TOKEN)
        assert raised.value.code == code, kwargs
        assert TOKEN not in str(raised.value)
    with pytest.raises(cf.CloudflareError, match="invalid_token"):
        await cf.async_verify_token(session, "")


async def test_zones_follow_pages_and_skip_bad_items(hass, aioclient_mock):
    """[KSM-TEST-404] Every page is read; names are lower-cased without a
    trailing dot; malformed items are skipped."""
    session = async_get_clientsession(hass)
    aioclient_mock.get(f"{API}/zones", params={"per_page": "50", "page": "1"}, json={
        "success": True, "result_info": {"total_pages": 2},
        "result": [_zone(1, "CFoxGA.com.", ("a.ns.test", "b.ns.test")), {"id": "x"}, "junk"]})
    aioclient_mock.get(f"{API}/zones", params={"per_page": "50", "page": "2"}, json={
        "success": True, "result_info": {"total_pages": 2}, "result": [_zone(2, "lab.cfoxga.com")]})
    zones = await cf.async_zones(session, TOKEN)
    assert zones == [cf.Zone("z1", "cfoxga.com", ("a.ns.test", "b.ns.test")),
                     cf.Zone("z2", "lab.cfoxga.com", ("a.ns.test",))]
    assert cf.find_zone(zones, "Portal.Lab.cfoxga.com.") == zones[1]
    assert cf.find_zone(zones, "cfoxga.com") == zones[0]
    assert cf.find_zone(zones, "notcfoxga.com") is None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/zones", params={"per_page": "50", "page": "1"}, text="not json{")
    with pytest.raises(cf.CloudflareError, match="cannot_connect"):
        await cf.async_zones(session, TOKEN)


async def test_txt_create_returns_the_record_and_delete_removes_it(hass, aioclient_mock):
    """[KSM-TEST-404] Create posts a 60 s TXT and returns its id; a reply with no
    id, a 403 and a refused delete raise fixed codes."""
    session = async_get_clientsession(hass)
    url = f"{API}/zones/z1/dns_records"
    aioclient_mock.post(url, json={"success": True, "result": {"id": "rec-9"}})
    assert await cf.async_create_txt(session, TOKEN, "z1", "_acme-challenge.p.cfoxga.com", "v") == "rec-9"
    assert aioclient_mock.mock_calls[-1][2] == {
        "type": "TXT", "name": "_acme-challenge.p.cfoxga.com", "content": "v", "ttl": 60}
    aioclient_mock.delete(f"{url}/rec-9", json={"success": True, "result": {"id": "rec-9"}})
    await cf.async_delete_txt(session, TOKEN, "z1", "rec-9")
    aioclient_mock.clear_requests()
    aioclient_mock.post(url, json={"success": True, "result": {}})
    with pytest.raises(cf.CloudflareError, match="api_error"):
        await cf.async_create_txt(session, TOKEN, "z1", "n", "v")
    aioclient_mock.clear_requests()
    aioclient_mock.post(url, status=403, json={"success": False})
    with pytest.raises(cf.CloudflareError, match="invalid_token"):
        await cf.async_create_txt(session, TOKEN, "z1", "n", "v")
    aioclient_mock.delete(f"{url}/rec-9", json={"success": False, "errors": [{"message": TOKEN}]})
    with pytest.raises(cf.CloudflareError) as raised:
        await cf.async_delete_txt(session, TOKEN, "z1", "rec-9")
    assert raised.value.code == "api_error" and TOKEN not in str(raised.value)
