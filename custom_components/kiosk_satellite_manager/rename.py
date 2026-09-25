"""Dedicated rename helper for `kiosk_satellite_manager.rename_device`
(KSM-BEHAVE-084/085, `#48`, `docs/SPEC/rename.md`).

Name derivation, the Kiosk Satellite settings layer, the Android system-name
layer, the DNS-host-migration layer, and the ESPHome action re-registration
layer (KSM-BEHAVE-092, `#56`) each live here so `__init__.py`'s service
handler stays orchestration only.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import ipaddress
import json
import re
import socket
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

from .ks_api_client import get_settings
from .provisioning import apply_provisioning, fetch_health

_SLUG_COLLAPSE_RE = re.compile(r"[^a-z0-9]+")

# KSM-BEHAVE-092: how long each ESPHome wait (new device_name recorded,
# actions re-registered) may take before the result reports `pending`.
ESPHOME_RENAME_TIMEOUT = 30.0
_WAIT_INTERVAL = 0.5


@dataclass(frozen=True)
class RenameNames:
    device_name: str
    hostname: str
    esphome_node_name: str


def derive_rename_names(display_name: str) -> RenameNames:
    """KSM-BEHAVE-084: derive `device.name`/hostname/`esphome.node_name`
    from one operator-supplied display name. Raises ValueError for a name
    that produces an empty slug (e.g. all punctuation/whitespace)."""
    device_name = display_name.strip()
    if not device_name:
        raise ValueError("name must not be blank")
    hostname = _SLUG_COLLAPSE_RE.sub("-", device_name.lower()).strip("-")
    if not hostname:
        raise ValueError("name must contain at least one letter or digit")
    return RenameNames(
        device_name=device_name,
        hostname=hostname,
        # #56: the node name is the DNS label itself. KS slugs whatever it is
        # given to hyphens before it goes on the wire; the underscore form is
        # only HA's action-name prefix (see `action_prefix`).
        esphome_node_name=hostname,
    )


async def apply_rename_ks_settings(
    session: aiohttp.ClientSession, host: str, token: str, names: RenameNames
) -> str:
    """KSM-BEHAVE-084: apply the derived settings over the same
    `PATCH /api/settings` + `/api/health` readback path `provision` uses.
    Returns "unchanged" without a PATCH when the device's current settings
    already hold every derived value, making a repeated call idempotent.
    `/api/health` has no node name, so it cannot decide that on its own
    (#56). Raises `ProvisioningMismatch`/`KsApiError` on a real failure --
    the caller decides how a failed KS layer affects the overall result."""
    target = {
        "device.name": names.device_name,
        "device.hostname": names.hostname,
        "esphome.node_name": names.esphome_node_name,
    }
    current = await get_settings(session, host, token)
    if all(current.get(key) == value for key, value in target.items()):
        return "unchanged"
    await apply_provisioning(session, host, token, target)
    return "applied"


async def set_android_device_name(names: RenameNames) -> str:
    """KSM-BEHAVE-084: set and read back Android's system device name using
    a verified supported method.

    No non-ADB write/readback method for the Android system device name has
    been verified (`docs/SPEC/rename.md` Section 7 Open Issues), and KSM
    `#47` prohibits routine post-onboarding ADB -- there is no supported
    method to call yet. Always report the honest "unsupported" result rather
    than a silent no-op that could be read as success; replace this body
    once a supported method is verified and wired in."""
    return "unsupported"


def _is_ip_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def derive_dns_host(current_host: str, hostname: str) -> str | None:
    """KSM-BEHAVE-085: replace only the first label of a DNS host, keeping
    its suffix. Returns None for an IP-address host (left unchanged, KSM
    has no DNS authority to rename it) or a host with no separator."""
    if _is_ip_host(current_host):
        return None
    labels = current_host.split(".")
    if len(labels) < 2:
        return None
    labels[0] = hostname
    return ".".join(labels)


async def _resolve_host(hass, host: str) -> str | None:
    try:
        return await hass.async_add_executor_job(socket.gethostbyname, host)
    except OSError:
        return None


async def resolve_and_verify_dns_host(
    hass, session: aiohttp.ClientSession, current_host: str, candidate_host: str
) -> bool:
    """KSM-BEHAVE-085: True only when `candidate_host` resolves to the same
    IP as `current_host` AND `/api/health` answers there -- never swap the
    working route on a DNS alias that has not been provisioned yet or that
    points at a different device."""
    current_ip = current_host if _is_ip_host(current_host) else await _resolve_host(hass, current_host)
    candidate_ip = await _resolve_host(hass, candidate_host)
    if current_ip is None or candidate_ip is None or current_ip != candidate_ip:
        return False
    try:
        await fetch_health(session, candidate_host)
    except (aiohttp.ClientError, TimeoutError):
        return False
    return True


ESPHOME_DOMAIN = "esphome"
_ESPHOME_DEVICE_NAME = "device_name"


def action_prefix(node_name: str) -> str:
    """HA's ESPHome integration names a node's actions
    `esphome.<node with - as _>_<action>` (`build_service_name`)."""
    return node_name.replace("-", "_")


@dataclass(frozen=True)
class EsphomeLink:
    """KSM-BEHAVE-092: the kiosk's ESPHome config entry and what HA had
    registered for it before the rename. KSM stores this link only; it never
    merges devices with the ESPHome entry."""

    entry: ConfigEntry
    old_node: str
    old_actions: frozenset[str]


async def _host_ip(hass, host: str) -> str | None:
    if _is_ip_host(host):
        return host
    return await _resolve_host(hass, host)


def _node_actions(hass, prefix: str, other_prefixes: set[str]) -> frozenset[str]:
    """Action suffixes registered under `<prefix>_`, skipping any that belong
    to another ESPHome node whose prefix merely starts with this one
    (`old_device_annex_notification` is not `old_device`'s)."""
    services = hass.services.async_services().get(ESPHOME_DOMAIN, {})
    lead = f"{prefix}_"
    longer = [f"{other}_" for other in other_prefixes if other.startswith(lead)]
    return frozenset(
        name[len(lead):]
        for name in services
        if name.startswith(lead) and not any(name.startswith(o) for o in longer)
    )


def _other_prefixes(hass, entry: ConfigEntry) -> set[str]:
    return {
        action_prefix(other.data[_ESPHOME_DEVICE_NAME])
        for other in hass.config_entries.async_entries(ESPHOME_DOMAIN)
        if other.entry_id != entry.entry_id and other.data.get(_ESPHOME_DEVICE_NAME)
    }


async def find_esphome_link(hass, ksm_host: str) -> EsphomeLink | None:
    """KSM-BEHAVE-092: the single enabled ESPHome entry whose host resolves to
    the same IP as the KSM entry's host. None when there is no match or more
    than one -- KSM never guesses which entry to reload."""
    candidates = [
        entry
        for entry in hass.config_entries.async_entries(ESPHOME_DOMAIN)
        if entry.disabled_by is None and entry.data.get("host")
    ]
    if not candidates:
        return None
    kiosk_ip = await _host_ip(hass, ksm_host)
    if kiosk_ip is None:
        return None
    matches = [entry for entry in candidates if await _host_ip(hass, entry.data["host"]) == kiosk_ip]
    if len(matches) != 1:
        return None
    entry = matches[0]
    old_node = entry.data.get(_ESPHOME_DEVICE_NAME) or ""
    old_actions = (
        _node_actions(hass, action_prefix(old_node), _other_prefixes(hass, entry)) if old_node else frozenset()
    )
    return EsphomeLink(entry=entry, old_node=old_node, old_actions=old_actions)


def find_action_callers(hass, prefix: str, suffixes: frozenset[str]) -> list[str]:
    """Automation and script entity IDs whose stored config names one of the
    node's old actions. Reported only; KSM never edits them."""
    names = [f"{ESPHOME_DOMAIN}.{prefix}_{suffix}" for suffix in suffixes]
    if not names:
        return []
    callers: list[str] = []
    for domain in ("automation", "script"):
        component = hass.data.get(domain)
        for entity in getattr(component, "entities", ()):
            raw = getattr(entity, "raw_config", None)
            if raw is None:
                continue
            text = json.dumps(raw, default=str)
            if any(name in text for name in names):
                callers.append(entity.entity_id)
    return sorted(callers)


async def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_WAIT_INTERVAL)
    return True


async def rename_esphome_actions(
    hass, link: EsphomeLink, new_node: str
) -> tuple[str, dict | None]:
    """KSM-BEHAVE-092: after a verified KS rename, reload the linked ESPHome
    entry so HA registers the node's actions under the new name. HA only
    re-registers an action whose definition changed, so without the reload
    the old names linger and the new ones never appear.

    HA also never unregisters the old names on reload; they stay bound to
    the unloaded connection until HA restarts. Once every recorded action is
    back under the new prefix, the old names are removed.

    Returns the `esphome` result and, when the node changed, the
    `esphome_actions` report."""
    new_prefix = action_prefix(new_node)
    others = _other_prefixes(hass, link.entry)
    renamed = link.old_node != new_node
    if not renamed and _node_actions(hass, new_prefix, others):
        return "unchanged", None

    actions = None
    if renamed:
        old_prefix = action_prefix(link.old_node)
        actions = {
            "old_prefix": old_prefix,
            "new_prefix": new_prefix,
            "callers": find_action_callers(hass, old_prefix, link.old_actions),
            "removed": [],
        }

    # KS restarts its ESPHome server on a node change and HA records the new
    # device_name when it reconnects. Reloading before that would register
    # the actions under the old name again. If it never happens, reload
    # anyway: the fresh connection reads the current name.
    await _wait_until(
        lambda: link.entry.data.get(_ESPHOME_DEVICE_NAME) == new_node, ESPHOME_RENAME_TIMEOUT
    )
    try:
        reloaded = await hass.config_entries.async_reload(link.entry.entry_id)
    except HomeAssistantError:
        return "failed", actions
    if reloaded is False:
        return "failed", actions

    expected = link.old_actions if renamed else frozenset()
    if not await _wait_until(
        lambda: expected <= _node_actions(hass, new_prefix, others), ESPHOME_RENAME_TIMEOUT
    ):
        return "pending", actions
    if actions is not None and actions["old_prefix"] != new_prefix:
        replaced = link.old_actions & _node_actions(hass, new_prefix, others)
        for suffix in sorted(replaced):
            name = f"{actions['old_prefix']}_{suffix}"
            hass.services.async_remove(ESPHOME_DOMAIN, name)
            actions["removed"].append(f"{ESPHOME_DOMAIN}.{name}")
    return "applied", actions
