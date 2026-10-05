"""The KSM panel's admin boundary and allowlisted tree (KSM-TEST-381–384)."""

import json
import asyncio
from types import MappingProxyType
from pathlib import Path
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager.const import DOMAIN
from custom_components.kiosk_satellite_manager.panel import PANEL_NAME
from custom_components.kiosk_satellite_manager.websocket_api import build_tree
from custom_components.kiosk_satellite_manager import diagnostics
from custom_components.kiosk_satellite_manager import fleet
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

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
    assert [child["title"] for child in tree["children"]] == ["Fleet", "Fleet", "Unmanaged"]
    assert tree["children"][1]["children"][0]["title"] == "Fleet - Zebra"
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


async def test_tree_keys_entities_by_unique_id_and_links_web_ui(hass):
    """[KSM-TEST-399] Lookup never depends on entity ID text; devices carry their Web UI URL."""
    parent = MockConfigEntry(domain=DOMAIN, title="Fleet - Lead",
                             data={"entry_type": "fleet", "leader_id": "ks-lead"})
    parent.add_to_hass(hass)
    for sub_id, data in (
        ("pinned", {"host": "10.0.0.5", "tls_spki_sha256": "SECRET-PIN",
                    "_ksm_fleet_status": {"leading": True, "self_id": "ks-lead"}}),
        ("plain", {"host": "kiosk.local"}),
    ):
        hass.config_entries.async_add_subentry(parent, ConfigSubentry(
            data=MappingProxyType(data), subentry_id=sub_id, subentry_type="device",
            title=sub_id.title(), unique_id=sub_id,
        ))
    registry = er.async_get(hass)
    online = registry.async_get_or_create(
        "sensor", DOMAIN, f"{parent.entry_id}_fleet_online_count", config_entry=parent,
        suggested_object_id="online_count_2")
    backup = registry.async_get_or_create(
        "button", DOMAIN, "pinned_backup_config", config_entry=parent,
        config_subentry_id="pinned", suggested_object_id="renamed_by_operator")
    tree = build_tree(hass)
    fleet_node = tree["children"][0]
    assert online.entity_id == "sensor.online_count_2"
    assert fleet_node["keys"] == {"fleet_online_count": "sensor.online_count_2"}
    pinned, plain = fleet_node["children"]
    assert pinned["keys"] == {"backup_config": backup.entity_id}
    assert pinned["web_ui_url"] == "https://10.0.0.5:2324"
    assert plain["web_ui_url"] == "http://kiosk.local:2324"
    assert "SECRET-PIN" not in json.dumps(tree)


async def test_web_ui_address_by_name_and_fleet_badge(hass):
    """[KSM-TEST-400] The Web UI link uses KS's by-name admin address; fleets link their leader."""
    parent = MockConfigEntry(domain=DOMAIN, title="Fleet - Lead",
                             data={"entry_type": "fleet", "leader_id": "ks-lead"})
    parent.add_to_hass(hass)
    hass.config_entries.async_add_subentry(parent, ConfigSubentry(
        data=MappingProxyType({"host": "10.0.0.5", "password": "synthetic-secret",
                               "tls_spki_sha256": "a" * 64}),
        subentry_id="lead", subentry_type="device", title="Lead", unique_id="lead",
    ))
    unmanaged = MockConfigEntry(domain=DOMAIN, title="Unmanaged", data={"entry_type": "unmanaged"})
    unmanaged.add_to_hass(hass)
    status = {"ok": True, "data": {"self": {"id": "ks-lead"}, "leader": True, "followers": []}}

    async def poll(fleet_reply):
        async def command(_session, _host, _token, name, *, pin):
            if name == "fleetStatus":
                return status
            if isinstance(fleet_reply, Exception):
                raise fleet_reply
            return fleet_reply
        with patch("custom_components.kiosk_satellite_manager.ks_api_client.login",
                   new=AsyncMock(return_value="synthetic-token")), patch(
            "custom_components.kiosk_satellite_manager.ks_api_client.run_command", new=command,
        ), patch("custom_components.kiosk_satellite_manager.fleet.async_reconcile", new=AsyncMock()):
            await fleet.async_poll_device(hass, "lead")
        assert fleet.status_available(hass, "lead")
        tree = build_tree(hass)
        fleet_node = next(n for n in tree["children"] if n["kind"] == "fleet")
        return tree, fleet_node, fleet_node["children"][0]

    named = "https://portal.example.com:2324"
    tree, fleet_node, lead = await poll({"ok": True, "data": {"hostUrl": named}})
    assert lead["web_ui_url"] == named
    assert fleet_node["title"] == "Fleet"
    assert fleet_node["web_ui_url"] == named
    unmanaged_node = next(n for n in tree["children"] if n["kind"] == "unmanaged")
    assert unmanaged_node["title"] == "Unmanaged" and "web_ui_url" not in unmanaged_node
    assert "web_ui_url" not in tree

    for bad in (KsApiError("unknown command"), {"ok": True, "data": {"hostUrl": None}},
                {"ok": True, "data": {"hostUrl": "https://user@portal.example.com:2324"}},
                {"ok": True, "data": {"hostUrl": "https://portal.example.com:2324/admin"}},
                {"ok": True, "data": {"hostUrl": "javascript://portal.example.com"}},
                {"ok": False, "error": "nope"}):
        _tree, fleet_node, lead = await poll(bad)
        assert lead["web_ui_url"] == "https://10.0.0.5:2324", bad
        assert fleet_node["web_ui_url"] == "https://10.0.0.5:2324", bad

    current = parent.subentries["lead"]
    hass.config_entries.async_update_subentry(parent, current, data={
        **current.data, "_ksm_fleet_status": {**current.data["_ksm_fleet_status"], "leading": False}})
    fleet_node = next(n for n in build_tree(hass)["children"] if n["kind"] == "fleet")
    assert fleet_node["web_ui_url"] is None
