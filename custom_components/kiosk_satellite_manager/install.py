"""Shared install/launch/permission-grant/onboarding sequence for a Kiosk
Satellite device -- used by both the Install/Reinstall button and
(KSM-BEHAVE-012) the config flow's auto-install step, so the two never
drift apart.

KSM-BEHAVE-007: `pm install` alone leaves the app installed but not running.
Confirmed live against the Test Portal: after `pm install -r -g` succeeded,
`/api/health` on :2324 still refused connections, because nothing had ever
launched the activity -- `am start` is required before the sensor can ever
read anything but "unavailable".

KSM-BEHAVE-008: `pm install -r -g` only auto-grants manifest-declared
runtime permissions. Battery-optimization exemption and "display over other
apps" have no runtime-permission equivalent and can't be requested through
`pm install` at all. The two commands below were pulled verbatim from Kiosk
Satellite's own web wizard source (`wizard.js`, fetched live from the Test
Portal's :2324 web UI), which hardcodes them as the fallback for devices
with no on-device settings screen for either toggle -- not guessed.

KSM-BEHAVE-010/011: once the app is up, sync its admin password and Device
Name to what was chosen at config-flow time, then connect it to this HA
instance with a freshly minted long-lived access token -- following the
device's own on-device setup wizard's own call shapes (`api/setup/password`
sets password + device name together on first run; `PATCH /api/settings` +
a `haCheckConnection` command handle the HA link either way, live-extracted
from wizard.js/settings.js). Best-effort: an entry created before this field
existed has no password (skipped, logged at debug), and a sync failure
(e.g. the web UI hasn't finished booting yet after `am start`) is logged as
a warning rather than failing the whole install/reinstall -- the app is
already installed and running by that point, which is the primary thing the
button promises.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from datetime import timedelta

import aiohttp
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import get_url

from typing import Any, Final

from . import ks_api_client
from .adb_client import AdbClient
from .const import (
    HA_TOKEN_LIFESPAN_DAYS,
    KS_APK_REMOTE_PATH,
    KS_MAIN_ACTIVITY,
    KS_PACKAGE,
    SYNC_STATUS_POLL_ATTEMPTS,
    SYNC_STATUS_POLL_DELAY_S,
)
from .device_profiles import DeviceProfile, get_profile
from .ks_api import latest_apk_url
from .ks_api_client import KsApiError

_LOGGER = logging.getLogger(__name__)

PORTAL_PERMISSIONS: Final = [
    "android.permission.RECORD_AUDIO",
    "android.permission.CAMERA",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.READ_EXTERNAL_STORAGE",
    "android.permission.WRITE_EXTERNAL_STORAGE",
    "android.permission.READ_LOGS",
]
PORTAL_APPOPS: Final = [
    "SYSTEM_ALERT_WINDOW",
    "WRITE_SETTINGS",
    "GET_USAGE_STATS",
]


def _write_temp_apk(data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".apk")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


async def install_and_launch(
    hass: HomeAssistant,
    client: AdbClient,
    session: aiohttp.ClientSession,
    host: str | None = None,
    device_name: str | None = None,
    password: str | None = None,
    ha_token: str | None = None,
    home_launcher: bool = True,
    device_profile: str | None = None,
) -> str | None:
    """Fetch the latest KS APK matching the device's ABI, install it, launch
    it, and grant full permissions. If a password is configured on the entry,
    also sync the device's admin password/Device Name and connect it to this
    HA instance (KSM-BEHAVE-010/011/014/015/020). Returns the HA token used."""
    profile = get_profile(device_profile)
    abi = await client.getprop("ro.product.cpu.abi")
    try:
        sdk_str = await client.getprop("ro.build.version.sdk")
        sdk = int(sdk_str) if sdk_str.isdigit() else 29
    except Exception:
        sdk = 29

    apk_url = await latest_apk_url(session, abi)
    async with session.get(apk_url) as resp:
        resp.raise_for_status()
        data = await resp.read()
    tmp_path = await hass.async_add_executor_job(_write_temp_apk, data)
    try:
        await client.push(tmp_path, KS_APK_REMOTE_PATH)
        await client.shell(f"pm install -r -g {KS_APK_REMOTE_PATH}")
        await client.shell(f"rm -f {KS_APK_REMOTE_PATH}")
    finally:
        await hass.async_add_executor_job(os.unlink, tmp_path)

    await client.shell(f"am start -n {KS_MAIN_ACTIVITY}")
    perms = profile.permissions_for_sdk(sdk) if profile.key != "unknown" else PORTAL_PERMISSIONS
    for perm in perms:
        await client.shell(f"pm grant {KS_PACKAGE} {perm}")
    appops = profile.appops_for_sdk(sdk) if profile.key != "unknown" else PORTAL_APPOPS
    for op in appops:
        await client.shell(f"appops set {KS_PACKAGE} {op} allow")
    await client.shell(f"dumpsys deviceidle whitelist +{KS_PACKAGE}")
    if profile.is_portal or profile.key == "unknown":
        await client.shell(f"dpm set-active-admin {KS_PACKAGE}/.KioskAdminReceiver")
        await client.shell("settings put global package_verifier_enable 0")

    if password is None or host is None:
        _LOGGER.debug(
            "no admin password configured for %s; skipping device-name/HA auto-connect sync",
            host,
        )
        return None
    try:
        return await _sync_device_and_connect_ha(
            hass,
            session,
            host,
            device_name or host,
            password,
            ha_token=ha_token,
            home_launcher=home_launcher,
            profile=profile,
        )
    except (KsApiError, aiohttp.ClientError, asyncio.TimeoutError) as err:
        _LOGGER.warning("device-name/HA auto-connect sync failed for %s: %s", host, err)
        return None



async def _wait_for_setup_status(session: aiohttp.ClientSession, host: str) -> dict:
    last_err: Exception | None = None
    for attempt in range(SYNC_STATUS_POLL_ATTEMPTS):
        try:
            return await ks_api_client.get_setup_status(session, host)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            last_err = err
            if attempt < SYNC_STATUS_POLL_ATTEMPTS - 1:
                await asyncio.sleep(SYNC_STATUS_POLL_DELAY_S)
    raise KsApiError(f"setup status never became reachable: {last_err}")


async def _mint_ha_token(hass: HomeAssistant, client_name: str) -> str:
    """A fresh long-lived access token for the device to use, following the
    same auth-manager calls HA's own "Long-Lived Access Tokens" profile-page
    feature uses (components/auth's websocket_create_long_lived_access_token)."""
    user = await hass.auth.async_get_owner()
    refresh_token = await hass.auth.async_create_refresh_token(
        user,
        client_name=client_name,
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=HA_TOKEN_LIFESPAN_DAYS),
    )
    return hass.auth.async_create_access_token(refresh_token)


async def _sync_device_and_connect_ha(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    host: str,
    device_name: str,
    password: str,
    ha_token: str | None = None,
    home_launcher: bool = True,
    profile: DeviceProfile | None = None,
) -> str:
    status = await _wait_for_setup_status(session, host)
    if status.get("passwordNeeded", True):
        token = await ks_api_client.setup_password(session, host, password, device_name)
    else:
        token = await ks_api_client.login(session, host, password)
        if status.get("deviceName") != device_name:
            await ks_api_client.patch_settings(session, host, token, {"device.name": device_name})

    if not ha_token:
        # HA requires client names to be unique across refresh tokens. A
        # reset/reprovisioned kiosk may have left a revoked-but-stored token
        # with the same user-visible name, so retain that name for operators
        # and add a short opaque suffix for the token identity.
        client_name = f"Kiosk Satellite Manager - {device_name} [{uuid.uuid4().hex}]"
        ha_token = await _mint_ha_token(hass, client_name)
    ha_url = get_url(hass, prefer_external=False).rstrip("/")
    start_path = (
        profile.start_url_path
        if profile and profile.start_url_path
        else ("/portal" if (not profile or profile.is_portal or profile.key == "unknown") else "")
    )
    start_url = f"{ha_url}{start_path}" if start_path else ha_url
    settings_payload: dict[str, Any] = {
        "ha.url": ha_url,
        "ha.token": ha_token,
        "browser.start_url": start_url,
        "browser.ignore_ssl_errors": True,
    }

    if home_launcher:
        settings_payload["home.enabled"] = True

    await ks_api_client.patch_settings(session, host, token, settings_payload)
    if not await ks_api_client.check_ha_connection(session, host, token):
        _LOGGER.warning("Kiosk Satellite reported the HA connection check failed for %s", host)
    return ha_token
