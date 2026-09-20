"""Kiosk Satellite on-device web UI client (KSM-BEHAVE-010/011).

Endpoint shapes below were extracted live from the Test Portal's own served
JS bundles (main.js/core.js/wizard.js/settings.js at :2324/static/), not
guessed:

- `GET api/setup/status` (unauthenticated) -> {setupNeeded, passwordNeeded,
  deviceName, ...}.
- `POST api/setup/password` body {password, deviceName} (unauthenticated,
  only valid while passwordNeeded is true) -> {token, ...}. wizard.js's own
  device-rename step reuses this single call to set the admin password and
  Device Name together on first run.
- `POST /api/login` body {password} -> {token, ...} (unauthenticated).
- `PATCH /api/settings` body {<key>: <value>, ...}, Bearer token -- used for
  both `device.name` (once a password already exists -- wizard.js's rename
  step falls back to this when !passwordNeeded) and `ha.url`/`ha.token`
  (wizard.js's Home Assistant step).
- `POST /api/commands/haCheckConnection` body "{}" , Bearer token ->
  {ok, error} -- validates whatever ha.url/ha.token are currently stored,
  used by both the wizard and the Settings tab's own "Validate" button.
"""
from __future__ import annotations

import aiohttp

from .const import HEALTH_PORT, HEALTH_TIMEOUT_S


class KsApiError(Exception):
    """A Kiosk Satellite web-UI API call failed or was rejected."""


def _base_url(host: str) -> str:
    return f"http://{host}:{HEALTH_PORT}"


async def get_setup_status(session: aiohttp.ClientSession, host: str) -> dict:
    async with session.get(
        f"{_base_url(host)}/api/setup/status",
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def setup_password(
    session: aiohttp.ClientSession, host: str, password: str, device_name: str
) -> str:
    """First-run only (passwordNeeded is true): sets the admin password and
    Device Name together, returns the auth token."""
    async with session.post(
        f"{_base_url(host)}/api/setup/password",
        json={"password": password, "deviceName": device_name},
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        data = await resp.json()
        if not resp.ok:
            raise KsApiError(data.get("error") or f"HTTP {resp.status}")
        return data["token"]


async def login(session: aiohttp.ClientSession, host: str, password: str) -> str:
    async with session.post(
        f"{_base_url(host)}/api/login",
        json={"password": password},
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        data = await resp.json()
        if not resp.ok:
            raise KsApiError(data.get("error") or f"HTTP {resp.status}")
        return data["token"]


async def patch_settings(
    session: aiohttp.ClientSession, host: str, token: str, values: dict
) -> dict:
    async with session.patch(
        f"{_base_url(host)}/api/settings",
        json=values,
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        data = await resp.json()
        if not resp.ok:
            raise KsApiError(data.get("error") or f"HTTP {resp.status}")
        rejected = data.get("rejected") or []
        if any(key in rejected for key in values):
            raise KsApiError(f"device rejected settings: {rejected!r}")
        return data


async def check_ha_connection(session: aiohttp.ClientSession, host: str, token: str) -> bool:
    async with session.post(
        f"{_base_url(host)}/api/commands/haCheckConnection",
        data="{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_S),
    ) as resp:
        data = await resp.json()
        return bool(data.get("ok"))
