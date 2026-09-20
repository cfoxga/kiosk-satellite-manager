"""Kiosk Satellite provisioning payload: build, apply, and verify.

Design driven directly by docs/SPEC/provisioning.md Verified Findings 1-3:
the whole payload rides one `ks.provision` ADB intent (Finding 1), the
device's own /api/health is the unauthenticated read-back channel
(Finding 2), and `am start` exit 0 proves nothing -- a malformed (e.g.
unescaped) payload is silently dropped while the shell still reports
success, so every apply is read back and compared (Finding 3).

The quoting in build_provision_command and the apply-then-readback sequence
in apply_provisioning were both live-verified this session against a
production device (docs/SPEC/provisioning.md): setting device.name via this
exact command, confirming the change via /api/health, then reverting it the
same way.
"""
from __future__ import annotations

import json
import logging

import aiohttp

from .adb_client import AdbClient
from .const import HEALTH_PORT, HEALTH_TIMEOUT_S, KS_MAIN_ACTIVITY

_LOGGER = logging.getLogger(__name__)

# Keys whose applied value can be verified directly against a same-named
# /api/health field. Anything outside this set (e.g. remote.password, which
# /api/health never echoes back) is applied but not read-back-verified here.
_HEALTH_VERIFIABLE = {
    "device.name": "name",
}


class ProvisioningMismatch(Exception):
    """The device's /api/health readback didn't match what we tried to set."""


def _quote_for_device_shell(payload_json: str) -> str:
    """Single-quote payload_json for the device's shell, escaping embedded
    single quotes -- Finding 3: an unquoted/word-split payload is silently
    dropped by Kiosk Satellite, not rejected, so getting this wrong fails
    silent, not loud."""
    return "'" + payload_json.replace("'", "'\\''") + "'"


def build_provision_command(payload: dict) -> str:
    """Build the `am start ... --es ks.provision '<json>'` shell command."""
    payload_json = json.dumps(payload)
    quoted = _quote_for_device_shell(payload_json)
    return f"am start -a android.intent.action.MAIN -n {KS_MAIN_ACTIVITY} --es ks.provision {quoted}"


async def fetch_health(session: aiohttp.ClientSession, host: str) -> dict:
    """GET /api/health -- unauthenticated per Finding 2."""
    async with session.get(
        f"http://{host}:{HEALTH_PORT}/api/health",
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def apply_provisioning(
    client: AdbClient, session: aiohttp.ClientSession, host: str, payload: dict
) -> dict:
    """Apply payload via one ks.provision intent, then verify what we can via
    /api/health. Raises ProvisioningMismatch if a verifiable key didn't take."""
    command = build_provision_command(payload)
    await client.shell(command)
    health = await fetch_health(session, host)
    mismatches = {}
    for key, value in payload.items():
        health_field = _HEALTH_VERIFIABLE.get(key)
        if health_field is None:
            continue
        if health.get(health_field) != value:
            mismatches[key] = (value, health.get(health_field))
    if mismatches:
        raise ProvisioningMismatch(
            f"payload not applied for: {mismatches!r} -- device may have word-split the intent"
        )
    return health
