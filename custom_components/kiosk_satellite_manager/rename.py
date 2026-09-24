"""Dedicated rename helper for `kiosk_satellite_manager.rename_device`
(KSM-BEHAVE-084/085, `#48`, `docs/SPEC/rename.md`).

Name derivation, the Kiosk Satellite settings layer, the Android system-name
layer, and the DNS-host-migration layer each live here so `__init__.py`'s
service handler stays orchestration only.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass

import aiohttp

from .provisioning import apply_provisioning, fetch_health

_SLUG_COLLAPSE_RE = re.compile(r"[^a-z0-9]+")


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
        esphome_node_name=hostname.replace("-", "_"),
    )


async def apply_rename_ks_settings(
    session: aiohttp.ClientSession, host: str, token: str, names: RenameNames
) -> str:
    """KSM-BEHAVE-084: apply the derived settings over the same
    `PATCH /api/settings` + `/api/health` readback path `provision` uses.
    Returns "unchanged" without a PATCH when the device already reports the
    target name, making a repeated call idempotent. Raises
    `ProvisioningMismatch`/`KsApiError` on a real failure -- the caller
    decides how a failed KS layer affects the overall result."""
    health = await fetch_health(session, host)
    if health.get("name") == names.device_name:
        return "unchanged"
    await apply_provisioning(
        session,
        host,
        token,
        {
            "device.name": names.device_name,
            "device.hostname": names.hostname,
            "esphome.node_name": names.esphome_node_name,
        },
    )
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
