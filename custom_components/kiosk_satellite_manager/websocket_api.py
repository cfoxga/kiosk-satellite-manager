"""Admin-only, allowlisted KSM panel tree subscription (KSM-BEHAVE-192)."""
from __future__ import annotations

import asyncio
from collections import defaultdict

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    CONF_ENTRY_TYPE, CONF_HOST, CONF_TLS_SPKI, DOMAIN, ENTRY_TYPE_MANAGER,
    ENTRY_TYPE_UNMANAGED, HEALTH_PORT,
)
from . import fleet
from . import web_ui

WS_TYPE = f"{DOMAIN}/subscribe_tree"


def _entities(hass, entry_id: str, subentry_id: str | None = None) -> dict:
    """Owned entity IDs by platform, plus unique-ID suffix -> entity ID (#198)."""
    groups: dict[str, list[str]] = defaultdict(list)
    keys: dict[str, str] = {}
    prefix = f"{subentry_id or entry_id}_"
    for row in er.async_entries_for_config_entry(er.async_get(hass), entry_id):
        if getattr(row, "config_subentry_id", None) != subentry_id:
            continue
        groups[row.domain].append(row.entity_id)
        if row.unique_id.startswith(prefix):
            keys[row.unique_id.removeprefix(prefix)] = row.entity_id
    return {"entities": {platform: sorted(ids) for platform, ids in groups.items()}, "keys": keys}


def _web_ui_url(device) -> str | None:
    host = device.data.get(CONF_HOST)
    if not host:
        return None
    scheme = "https" if device.data.get(CONF_TLS_SPKI) else "http"
    return f"{scheme}://{host}:{HEALTH_PORT}"


def _device_id(hass, entry_id: str, subentry_id: str | None = None) -> str | None:
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id):
        if any(domain == DOMAIN and ident == (subentry_id or entry_id)
               for domain, ident in device.identifiers):
            return device.id
    return None


def _repairs(hass, node_for_device: dict[str, dict], fleets: list[dict], root: dict) -> None:
    """Attach only visible KSM issues to one owner; never copy issue.data."""
    for (domain, issue_id), issue in ir.async_get(hass).issues.items():
        if domain != DOMAIN or getattr(issue, "ignored", False) or getattr(issue, "dismissed", False):
            continue
        owner = root
        for device_id, node in node_for_device.items():
            if issue_id.endswith(f"_{device_id}"):
                owner = node
                break
        if owner is root and issue_id.startswith("new_follower_"):
            leader_id = (issue.data or {}).get("leader_id")
            owner = next((node for node in fleets if node.get("leader_ks_id") == leader_id), root)
        owner["repairs"].append({
            "issue_id": issue_id,
            "translation_key": issue.translation_key or issue_id,
        })


def build_tree(hass) -> dict:
    """Build a fresh snapshot from HA-owned state with no entry data/options."""
    entries = hass.config_entries.async_entries(DOMAIN)
    manager = next((e for e in entries if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER), None)
    root = {
        "kind": "global", "entry_id": manager.entry_id if manager else None,
        "title": manager.title if manager else "KSM Settings",
        **(_entities(hass, manager.entry_id) if manager else {"entities": {}, "keys": {}}),
        "repairs": [], "children": [],
    }
    groups = [e for e in entries if e.data.get(CONF_ENTRY_TYPE) in ("fleet", ENTRY_TYPE_UNMANAGED)]
    groups.sort(key=lambda e: (e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_UNMANAGED, e.title.casefold()))
    node_for_device: dict[str, dict] = {}
    for entry in groups:
        unmanaged = entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_UNMANAGED
        group = {
            "kind": "unmanaged" if unmanaged else "fleet",
            "entry_id": entry.entry_id, "title": entry.title,
            **_entities(hass, entry.entry_id), "repairs": [], "children": [],
        }
        if not unmanaged:
            group["leader_ks_id"] = entry.data.get("leader_id")
        for device in fleet.device_entries(hass, entry):
            coordinator = hass.data.get(DOMAIN, {}).get(device.entry_id)
            status = device.fleet_status
            node = {
                "kind": "device", "entry_id": entry.entry_id,
                "subentry_id": device.entry_id, "title": device.title,
                "leader": status.get("leading") is True,
                "device_id": _device_id(hass, entry.entry_id, device.entry_id),
                **_entities(hass, entry.entry_id, device.entry_id),
                "web_ui_url": _web_ui_url(device),
                "online": bool(coordinator and coordinator.last_update_success),
                "repairs": [],
            }
            group["children"].append(node)
            node_for_device[device.entry_id] = node
        group["children"].sort(key=lambda n: (not n["leader"], n["title"].casefold()))
        root["children"].append(group)
    # HA retains discovery flows until accepted or dismissed.
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        if flow.context.get("source") != "integration_discovery":
            continue
        info = flow.init_data or {}
        leader_id = info.get("leader_id")
        if leader_id is None:
            for device in fleet.device_entries(hass):
                rows = device.fleet_status.get("follower_rows") or {}
                if info.get("ks_id") in rows and device.fleet_status.get("leading"):
                    leader_id = device.fleet_status.get("self_id")
                    break
        target = next((g for g in root["children"]
                       if leader_id is not None and g.get("leader_ks_id") == leader_id), None)
        if target is None:
            target = next((g for g in root["children"] if g["kind"] == "unmanaged"), root)
        target["children"].append({
            "kind": "offer", "entry_id": target.get("entry_id"),
            "title": info.get("name") or "Discovered follower",
            "flow_id": flow.flow_id, "entities": {}, "keys": {}, "repairs": [],
        })
    _repairs(hass, node_for_device, root["children"], root)
    for group in root["children"]:
        group.pop("leader_ks_id", None)
    return root


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE})
@websocket_api.require_admin
@callback
def handle_subscribe_tree(hass, connection, msg):
    """Subscribe once; coalesce relevant HA changes to one event per second."""
    loop = asyncio.get_running_loop()
    timer = None
    stops = []
    coordinator_stops = {}
    known_entity_ids: set[str] = set()
    known_device_ids: set[str] = set()

    @callback
    def watch_coordinators():
        live = hass.data.get(DOMAIN, {})
        for key in tuple(coordinator_stops):
            if key not in live:
                coordinator_stops.pop(key)()
        for key, coordinator in live.items():
            if key not in coordinator_stops:
                coordinator_stops[key] = coordinator.async_add_listener(changed)

    @callback
    def send():
        nonlocal timer
        timer = None
        watch_coordinators()
        tree = build_tree(hass)
        known_entity_ids.clear()
        known_device_ids.clear()

        def remember(node):
            if node.get("device_id"):
                known_device_ids.add(node["device_id"])
            for ids in node.get("entities", {}).values():
                known_entity_ids.update(ids)
            for child in node.get("children", []):
                remember(child)

        remember(tree)
        connection.send_message(websocket_api.event_message(msg["id"], tree))

    @callback
    def changed(*_):
        nonlocal timer
        if timer is None:
            timer = loop.call_later(1, send)

    @callback
    def entry_changed(_change, entry):
        if entry.domain == DOMAIN:
            changed()

    @callback
    def registry_changed(event):
        entry_ids = {
            e.entry_id for e in hass.config_entries.async_entries(DOMAIN)
        }
        entity_id = event.data.get("entity_id")
        device_id = event.data.get("device_id")
        old_entity_id = event.data.get("old_entity_id")
        entity = er.async_get(hass).async_get(entity_id) if entity_id else None
        device = dr.async_get(hass).async_get(device_id) if device_id else None
        if (event.data.get("config_entry_id") in entry_ids
                or entity_id in known_entity_ids
                or old_entity_id in known_entity_ids
                or device_id in known_device_ids
                or entity and entity.config_entry_id in entry_ids
                or device and any(entry in entry_ids for entry in device.config_entries)):
            changed()

    @callback
    def repair_changed(_event):
        changed()

    stops.append(async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, entry_changed))
    stops.append(hass.config_entries.flow.async_subscribe_flow(lambda *_: changed()))
    stops.append(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_changed))
    stops.append(hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, registry_changed))
    stops.append(hass.bus.async_listen(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, repair_changed))
    watch_coordinators()

    @callback
    def unsubscribe():
        if timer is not None:
            timer.cancel()
        for stop in stops:
            stop()
        for stop in coordinator_stops.values():
            stop()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    send()


def async_register_websocket_command(hass):
    websocket_api.async_register_command(hass, handle_subscribe_tree)
    websocket_api.async_register_command(hass, handle_open_web_ui)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/open_web_ui",
    vol.Required("entry_id"): str,
    vol.Required("subentry_id"): str,
})
@websocket_api.require_admin
@callback
def handle_open_web_ui(hass, connection, msg):
    """Grant one admin a short-lived view of one managed kiosk."""
    url = web_ui.issue_grant(hass, connection.user, msg["entry_id"], msg["subentry_id"])
    if url is None:
        connection.send_error(msg["id"], "not_available", "Managed device is unavailable")
        return
    connection.send_result(msg["id"], {"url": url})
