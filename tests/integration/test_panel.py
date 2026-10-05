"""The KSM panel's admin boundary and allowlisted tree (KSM-TEST-381–384)."""

import json
import asyncio
import time
import aiohttp
from types import MappingProxyType
from pathlib import Path
import pytest
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager.const import DOMAIN
from custom_components.kiosk_satellite_manager.panel import PANEL_NAME
from custom_components.kiosk_satellite_manager.websocket_api import build_tree
from custom_components.kiosk_satellite_manager import diagnostics
from custom_components.kiosk_satellite_manager import web_ui
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError
from aiohttp import web

from .test_global_settings import _manager


async def test_manager_panel_lifecycle(hass, release_check):
    """[KSM-TEST-381] The manager alone owns one admin panel."""
    manager = await _manager(hass)
    assert PANEL_NAME in hass.data["frontend_panels"]
    panel = hass.data["frontend_panels"][PANEL_NAME]
    assert panel.require_admin
    assert panel.sidebar_title == "Kiosk Satellite Manager"
    assert "?v=" in panel.config["_panel_custom"]["module_url"]
    assert await hass.config_entries.async_unload(manager.entry_id)
    assert PANEL_NAME not in hass.data["frontend_panels"]
    assert await hass.config_entries.async_setup(manager.entry_id)
    assert PANEL_NAME in hass.data["frontend_panels"]


async def test_tree_orders_nodes_and_excludes_secrets(hass):
    """[KSM-TEST-383/384] Fleet order and snapshot exposure are explicit."""
    manager = MockConfigEntry(domain=DOMAIN, title="KSM Settings", data={"entry_type": "manager"})
    manager.add_to_hass(hass)
    for title in ("Fleet - Zebra", "Fleet - Alpha", "Unmanaged"):
        kind = "unmanaged" if title == "Unmanaged" else "fleet"
        parent = MockConfigEntry(domain=DOMAIN, title=title, data={"entry_type": kind})
        parent.add_to_hass(hass)
        if kind == "fleet":
            hass.config_entries.async_add_subentry(parent, ConfigSubentry(
                data=MappingProxyType({"password": "SECRET-PASSWORD", "ha_token": "SECRET-TOKEN",
                                       "tls_spki_sha256": "SECRET-PIN", "key_path": "SECRET-KEY",
                                       "_ksm_fleet_status": {"leading": True}}),
                subentry_id=f"device-{title}", subentry_type="device", title=title,
                unique_id=f"device-{title}",
            ))
            hass.config_entries.async_add_subentry(parent, ConfigSubentry(
                data=MappingProxyType({}), subentry_id=f"follower-{title}",
                subentry_type="device", title="A follower", unique_id=f"follower-{title}",
            ))
    offers = [SimpleNamespace(context={"source": "integration_discovery"},
                              init_data={"ks_id": "unknown", "name": "Pending follower"},
                              flow_id="offer-flow")]
    with patch.object(hass.config_entries.flow, "async_progress_by_handler", return_value=offers):
        tree = build_tree(hass)
    assert tree["kind"] == "global"
    assert [child["title"] for child in tree["children"]] == [
        "Fleet - Alpha", "Fleet - Zebra", "Unmanaged"
    ]
    assert [child["title"] for child in tree["children"][0]["children"]] == [
        "Fleet - Alpha", "A follower"
    ]
    assert tree["children"][2]["children"][0]["kind"] == "offer"
    encoded = json.dumps(tree)
    for marker in ("SECRET-PASSWORD", "SECRET-TOKEN", "SECRET-PIN", "SECRET-KEY"):
        assert marker not in encoded
    assert '"data"' not in encoded and '"options"' not in encoded


async def test_repair_ownership_and_component_isolation(hass):
    """[KSM-TEST-382/392] Device repairs stay scoped and frontend has no HAM dependency."""
    manager = MockConfigEntry(domain=DOMAIN, title="KSM Settings", data={"entry_type": "manager"})
    manager.add_to_hass(hass)
    parent = MockConfigEntry(domain=DOMAIN, title="Unmanaged", data={"entry_type": "unmanaged"})
    parent.add_to_hass(hass)
    hass.config_entries.async_add_subentry(parent, ConfigSubentry(
        data=MappingProxyType({}), subentry_id="device-a", subentry_type="device",
        title="Device A", unique_id="device-a",
    ))
    for domain, ident in ((DOMAIN, "tls_disabled_device-a"), (DOMAIN, "unrecognized"),
                          ("other_domain", "foreign")):
        ir.async_create_issue(hass, domain, ident, is_fixable=True,
                              severity=ir.IssueSeverity.WARNING, translation_key="test")
    tree = build_tree(hass)
    assert [issue["issue_id"] for issue in tree["children"][0]["children"][0]["repairs"]] == [
        "tls_disabled_device-a"
    ]
    assert [issue["issue_id"] for issue in tree["repairs"]] == ["unrecognized"]
    source = Path(__file__).parents[2] / "custom_components" / DOMAIN / "www"
    scripts = "\n".join(path.read_text() for path in source.rglob("*.js") if "lit/" not in str(path))
    for forbidden in ("/api/ham", "ham/", "ham-"):
        assert forbidden not in scripts


async def test_device_diagnostics_download_has_scoped_data(hass):
    """[KSM-TEST-391] Device download resolves the HA device it was opened for."""
    parent = MockConfigEntry(domain=DOMAIN, title="Unmanaged", data={"entry_type": "unmanaged"})
    parent.add_to_hass(hass)
    hass.config_entries.async_add_subentry(parent, ConfigSubentry(
        data=MappingProxyType({"device_profile": "portal_mini", "password": "SECRET-PASSWORD"}),
        subentry_id="device-a", subentry_type="device", title="Device A", unique_id="device-a",
    ))
    device = dr.async_get(hass).async_get_or_create(config_entry_id=parent.entry_id,
                                                    identifiers={(DOMAIN, "device-a")})
    assert hasattr(diagnostics, "async_get_device_diagnostics")
    result = await diagnostics.async_get_device_diagnostics(hass, parent, device)
    assert len(result["devices"]) == 1
    assert "SECRET-PASSWORD" not in json.dumps(result)


async def test_tree_subscription_requires_admin(hass, hass_ws_client, hass_read_only_access_token, release_check):
    """[KSM-TEST-382] Non-admins get no tree; admins receive one snapshot."""
    manager = await _manager(hass)
    denied = await hass_ws_client(hass, hass_read_only_access_token)
    await denied.send_json({"id": 41, "type": f"{DOMAIN}/subscribe_tree"})
    rejection = await denied.receive_json()
    assert rejection["success"] is False
    assert rejection["error"]["code"] == "unauthorized"
    allowed = await hass_ws_client(hass)
    await allowed.send_json({"id": 42, "type": f"{DOMAIN}/subscribe_tree"})
    assert (await allowed.receive_json())["success"] is True
    snapshot = await allowed.receive_json()
    assert snapshot["type"] == "event"
    assert snapshot["event"]["kind"] == "global"
    for ident in ("burst_one", "burst_two", "burst_three"):
        ir.async_create_issue(hass, DOMAIN, ident, is_fixable=True,
                              severity=ir.IssueSeverity.WARNING, translation_key="test")
    updated = await asyncio.wait_for(allowed.receive_json(), timeout=3)
    assert updated["type"] == "event"
    assert len(updated["event"]["repairs"]) == 3
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(allowed.receive_json(), timeout=0.2)
    registry = er.async_get(hass)
    entity = registry.async_get_or_create("sensor", DOMAIN, "panel-probe", config_entry=manager)
    added = await asyncio.wait_for(allowed.receive_json(), timeout=3)
    assert entity.entity_id in added["event"]["entities"]["sensor"]
    registry.async_update_entity(entity.entity_id, new_entity_id="sensor.panel_renamed")
    renamed = await asyncio.wait_for(allowed.receive_json(), timeout=3)
    assert "sensor.panel_renamed" in renamed["event"]["entities"]["sensor"]
    registry.async_remove("sensor.panel_renamed")
    removed = await asyncio.wait_for(allowed.receive_json(), timeout=3)
    assert "sensor.panel_renamed" not in removed["event"]["entities"].get("sensor", [])
    await allowed.send_json({"id": 43, "type": "unsubscribe_events", "subscription": 42})
    assert (await allowed.receive_json())["success"] is True


async def test_web_ui_grant_requires_admin_and_managed_device(
    hass, hass_ws_client, hass_read_only_access_token, release_check,
):
    """[KSM-TEST-397] A browser can open only an admin-selected managed device."""
    await _manager(hass)
    parent = MockConfigEntry(domain=DOMAIN, title="Unmanaged", data={"entry_type": "unmanaged"})
    parent.add_to_hass(hass)
    hass.config_entries.async_add_subentry(parent, ConfigSubentry(
        data=MappingProxyType({"host": "192.0.2.10", "password": "device-secret"}),
        subentry_id="device-a", subentry_type="device", title="Device A", unique_id="device-a",
    ))
    denied = await hass_ws_client(hass, hass_read_only_access_token)
    command = {"type": f"{DOMAIN}/open_web_ui", "entry_id": parent.entry_id,
               "subentry_id": "device-a"}
    await denied.send_json({"id": 71, **command})
    assert (await denied.receive_json())["error"]["code"] == "unauthorized"
    admin = await hass_ws_client(hass)
    await admin.send_json({"id": 72, **command})
    result = await admin.receive_json()
    assert result["success"] is True
    assert result["result"]["url"].startswith(f"/api/{DOMAIN}/web/")
    assert "192.0.2.10" not in str(result)
    assert "device-secret" not in str(result)
    grant = result["result"]["url"].rstrip("/").split("/")[-1]
    assert (await web_ui._resolve(hass, grant)).entry_id == "device-a"
    web_ui._grants(hass)[grant].expires = time.monotonic() - 1
    with pytest.raises(web.HTTPForbidden):
        await web_ui._resolve(hass, grant)
    web_ui._grants(hass)[grant].expires = time.monotonic() + 3600
    with patch.object(hass.auth, "async_get_user", return_value=SimpleNamespace(
        is_active=True, is_admin=False,
    )):
        with pytest.raises(web.HTTPForbidden):
            await web_ui._resolve(hass, grant)
    await admin.send_json({"id": 73, **{**command, "subentry_id": "missing"}})
    assert (await admin.receive_json())["success"] is False
    hass.config_entries.async_remove_subentry(parent, "device-a")
    with pytest.raises(web.HTTPForbidden):
        await web_ui._resolve(hass, grant)
    web_ui.async_unload(hass)
    assert grant not in web_ui._grants(hass)


def test_web_ui_proxy_path_and_document_isolation():
    """[KSM-TEST-397/398] The proxy keeps a relative path and isolates JS storage."""
    assert web_ui._path("static/main.js", "v=1") == "/static/main.js?v=1"
    assert web_ui._credential_url("192.0.2.10", "/api/settings", "aa" * 32) == "https://192.0.2.10:2324/api/settings"
    for path in ("../api", "static/../api", "static\\api", "static/\x00"):
        with pytest.raises(web.HTTPBadRequest):
            web_ui._path(path, "")
    document = web_ui._bootstrap(b"<!doctype html><head><title>Kiosk</title></head>", "device-token")
    assert b"device-token" in document
    assert b"Object.defineProperty(window,'localStorage'" in document
    assert b"<title>Kiosk</title>" in document
    assert "sandbox allow-scripts" in web_ui._DOCUMENT_HEADERS["Content-Security-Policy"]
    assert "allow-same-origin" not in web_ui._DOCUMENT_HEADERS["Content-Security-Policy"]
    source = Path(__file__).parents[2] / "custom_components" / DOMAIN / "www" / "ksm-panel.js"
    assert 'sandbox="allow-scripts allow-forms allow-downloads"' in source.read_text()


async def test_web_ui_http_proxy_rejects_other_targets(
    hass, hass_client, release_check, monkeypatch,
):
    """[KSM-TEST-398] The view sends no HA credentials and refuses redirects."""
    await _manager(hass)
    parent = MockConfigEntry(domain=DOMAIN, title="Unmanaged", data={"entry_type": "unmanaged"})
    parent.add_to_hass(hass)
    hass.config_entries.async_add_subentry(parent, ConfigSubentry(
        data=MappingProxyType({"host": "192.0.2.10", "password": "device-secret"}),
        subentry_id="device-a", subentry_type="device", title="Device A", unique_id="device-a",
    ))
    user = next(user for user in await hass.auth.async_get_users() if user.is_admin)
    url = web_ui.issue_grant(hass, user, parent.entry_id, "device-a")
    assert url
    seen = []

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "text/html"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def read(self):
            return b"<!doctype html><head><title>Kiosk</title></head>"

        @property
        def content(self):
            return self

        async def iter_chunked(self, _size):
            yield b'{"ok":true}'

    class FakeSession:
        def request(self, method, target, **kwargs):
            seen.append((method, target, kwargs))
            return FakeResponse()

    async def fake_login(*_args, **_kwargs):
        return "device-token"

    monkeypatch.setattr(web_ui, "login", fake_login)
    monkeypatch.setattr(web_ui, "async_get_clientsession", lambda _hass: FakeSession())
    client = await hass_client()
    response = await client.get(url, headers={"Authorization": ""})
    assert response.status == 200
    body = await response.text()
    assert "device-token" in body and "device-secret" not in body
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "allow-same-origin" not in response.headers["Content-Security-Policy"]
    assert seen[0][1] == "http://192.0.2.10:2324/"
    assert "Authorization" not in seen[0][2]["headers"]
    assert "Cookie" not in seen[0][2]["headers"]
    assert seen[0][2]["allow_redirects"] is False
    authenticated = await client.post(
        url + "api/settings", headers={"Authorization": "Bearer device-token"}, json={},
    )
    assert authenticated.status == 200
    assert seen[-1][2]["headers"]["Authorization"] == "Bearer device-token"
    assert (await client.post(
        url + "api/settings", headers={"Authorization": "Bearer ha-token"}, json={},
    )).status == 403
    assert (await client.get(f"/api/{DOMAIN}/web/invalid/", headers={"Authorization": ""})).status == 403
    assert (await client.options(url + "api/settings", headers={"Origin": "null"})).status == 204
    assert (await client.options(url + "api/settings", headers={"Origin": "https://evil.example"})).status == 403
    FakeResponse.status = 302
    assert (await client.get(url, headers={"Authorization": ""})).status == 502
    FakeResponse.status = 200

    async def rejected_login(*_args, **_kwargs):
        raise KsApiError("wrong password")

    monkeypatch.setattr(web_ui, "login", rejected_login)
    rejected = await client.get(url, headers={"Authorization": ""})
    assert rejected.status == 502
    assert "wrong password" not in await rejected.text()


async def test_open_web_ui_socket_closes_when_grant_revoked(
    hass, hass_client, release_check, monkeypatch,
):
    """[KSM-TEST-397/398] Revoking a grant closes an active device control socket."""
    await _manager(hass)
    parent = MockConfigEntry(domain=DOMAIN, title="Unmanaged", data={"entry_type": "unmanaged"})
    parent.add_to_hass(hass)
    hass.config_entries.async_add_subentry(parent, ConfigSubentry(
        data=MappingProxyType({"host": "192.0.2.10", "password": "device-secret"}),
        subentry_id="device-a", subentry_type="device", title="Device A", unique_id="device-a",
    ))
    user = next(user for user in await hass.auth.async_get_users() if user.is_admin)
    url = web_ui.issue_grant(hass, user, parent.entry_id, "device-a")
    assert url
    grant = url.rstrip("/").split("/")[-1]
    web_ui._grants(hass)[grant].device_token = "device-token"
    targets = []

    class FakeUpstream:
        closed = False

        async def __aiter__(self):
            while not self.closed:
                await asyncio.sleep(100)
                yield None

        async def close(self):
            self.closed = True

    upstream = FakeUpstream()

    class FakeSession:
        async def ws_connect(self, target, **kwargs):
            targets.append((target, kwargs))
            return upstream

    monkeypatch.setattr(web_ui, "async_get_clientsession", lambda _hass: FakeSession())
    client = await hass_client()
    socket = await client.ws_connect(
        url + "api/ws?token=device-token", headers={"Origin": "null", "Authorization": ""},
    )
    assert targets[0][0] == "ws://192.0.2.10:2324/api/ws?token=device-token"
    web_ui.async_unload(hass)
    result = await socket.receive(timeout=7)
    assert result.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    assert upstream.closed
    await client.close()
    await hass.async_block_till_done()
