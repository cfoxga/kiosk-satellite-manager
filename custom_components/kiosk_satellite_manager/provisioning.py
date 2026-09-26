"""Kiosk Satellite provisioning payload: build, apply and verify.

*Superseded 2026-09-24 (`#47`, KSM-BEHAVE-083)*: the `provision` **service**
now rides one `PATCH /api/settings` over Kiosk Satellite's own `:2324` API
(pinned HTTPS when configured; HTTP otherwise). `ks_api_client.patch_settings`
raises on per-key rejection. This replaces the `ks.provision` ADB intent;
KSM-BEHAVE-081 keeps ADB only for onboarding, Install/Reinstall and Uninstall. `build_provision_command`
is still used directly by install.py's onboarding sequence (setting
`home.enabled` during the Install/Reinstall button's own ADB session, which
KSM-BEHAVE-081 permits), so it stays here rather than being deleted. The
device's own /api/health remains the unauthenticated read-back channel for
the keys it echoes (originally Verified Finding 2, docs/SPEC/provisioning.md);
reading it back after a PATCH costs nothing and catches a device-side
application bug that a 200 alone would not.
"""
from __future__ import annotations

import json
import logging

import aiohttp

from . import ks_api_client
from .const import KS_MAIN_ACTIVITY

_LOGGER = logging.getLogger(__name__)


def _quote_for_device_shell(payload_json: str) -> str:
    """Single-quote payload_json for the device's shell, escaping embedded
    single quotes -- an unquoted/word-split payload is silently dropped by
    Kiosk Satellite, not rejected, so getting this wrong fails silent, not
    loud."""
    return "'" + payload_json.replace("'", "'\\''") + "'"


def build_provision_command(payload: dict) -> str:
    """Build the `am start ... --es ks.provision '<json>'` shell command
    still used by install.py's onboarding sequence."""
    payload_json = json.dumps(payload)
    quoted = _quote_for_device_shell(payload_json)
    return f"am start -a android.intent.action.MAIN -n {KS_MAIN_ACTIVITY} --es ks.provision {quoted}"

# Keys whose applied value can be verified directly against a same-named
# /api/health field. Anything outside this set (e.g. remote.password, which
# /api/health never echoes back) is applied but not read-back-verified here.
_HEALTH_VERIFIABLE = {
    "device.name": "name",
}


class ProvisioningMismatch(Exception):
    """The device's /api/health readback didn't match what we tried to set."""


async def fetch_health(session: aiohttp.ClientSession, host: str, *, pin: str | None) -> dict:
    """GET /api/health -- unauthenticated, pinned HTTPS when the entry has a
    pin (KSM-BEHAVE-093)."""
    return await ks_api_client.get_health(session, host, pin=pin)


async def apply_provisioning(
    session: aiohttp.ClientSession, host: str, token: str, payload: dict, *, pin: str | None
) -> dict:
    """Apply payload via one PATCH /api/settings (KSM-BEHAVE-083), then
    verify what we can via /api/health. `ks_api_client.patch_settings`
    already raises KsApiError naming any per-key rejection.
    Raises ProvisioningMismatch if a verifiable key didn't take."""
    await ks_api_client.patch_settings(session, host, token, payload, pin=pin)
    health = await fetch_health(session, host, pin=pin)
    mismatches = {}
    for key, value in payload.items():
        health_field = _HEALTH_VERIFIABLE.get(key)
        if health_field is None:
            continue
        if health.get(health_field) != value:
            mismatches[key] = (value, health.get(health_field))
    if mismatches:
        raise ProvisioningMismatch(f"payload not applied for: {mismatches!r}")
    return health
