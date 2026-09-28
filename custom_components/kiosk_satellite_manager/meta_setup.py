"""KSM-BEHAVE-112 (#54): put a Portal's Meta login back after Device Owner.

The account purge that Device Owner enrollment needs also wipes the Portal's
Meta identity (docs/SPEC/device-management-strategy.md section 6). KSM:

1. turns Kiosk Satellite's kiosk lock off over its settings API -- under
   Device Owner the lock allowlists only Kiosk Satellite, so Meta's setup
   screen could never come to the front;
2. resets and launches Meta setup over ADB (`device_owner.restart_meta_setup`);
3. watches, over one held ADB connection, for the Meta login accounts to come
   back, then turns the lock settings it changed back on and says so.

A person finishes setup and the WhatsApp login on the Portal; KSM cannot.
Every outcome is reported in the device's Device Owner notification.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import device_owner, ks_api_client
from .adb_client import AdbClient
from .const import DOMAIN
from .ks_api_client import KsApiError

_LOGGER = logging.getLogger(__name__)

# Either one pins Kiosk Satellite (KS kiosk_manager: kiosk.enabled || lockdown).
LOCK_SETTINGS = ("kiosk.enabled", "lockdown.enabled")
WATCH_INTERVAL_S = 15.0
WATCH_TIMEOUT_S = 1800.0

_API_ERRORS = (KsApiError, aiohttp.ClientError, TimeoutError, ValueError, KeyError)


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    key_path: str
    password: str | None
    pin: str | None
    model_key: str | None
    name: str


def notify(hass: HomeAssistant, target: Target, message: str) -> None:
    persistent_notification.async_create(
        hass,
        message=f"{target.name}: {message}",
        title="Kiosk Satellite Device Owner",
        notification_id=f"{DOMAIN}_device_owner_{target.host}",
    )


async def _set_lock(hass: HomeAssistant, target: Target, values: dict) -> None:
    session = async_get_clientsession(hass)
    token = await ks_api_client.login(session, target.host, target.password, pin=target.pin)
    await ks_api_client.patch_settings(session, target.host, token, values, pin=target.pin)


async def kiosk_lock_off(hass: HomeAssistant, target: Target) -> tuple[str, ...] | None:
    """Turn off each LOCK_SETTINGS entry that is on; return the ones turned
    off. None when the settings API could not be used."""
    if not target.password:
        return None
    session = async_get_clientsession(hass)
    try:
        token = await ks_api_client.login(
            session, target.host, target.password, pin=target.pin
        )
        current = await ks_api_client.get_settings(session, target.host, token, pin=target.pin)
        on = tuple(k for k in LOCK_SETTINGS if current.get(k) is True)
        if on:
            await ks_api_client.patch_settings(
                session, target.host, token, dict.fromkeys(on, False), pin=target.pin
            )
    except _API_ERRORS as err:
        _LOGGER.warning("Could not turn off the kiosk lock on %s: %s", target.name, err)
        return None
    return on


async def restore_kiosk_lock(hass: HomeAssistant, target: Target, keys: tuple[str, ...]) -> bool:
    if not keys:
        return True
    try:
        await _set_lock(hass, target, dict.fromkeys(keys, True))
    except _API_ERRORS as err:
        _LOGGER.warning("Could not turn the kiosk lock back on for %s: %s", target.name, err)
        return False
    return True


async def async_start(
    hass: HomeAssistant, target: Target, client: device_owner.ShellClient
) -> None:
    """Lock off, Meta setup on screen, watcher started. Raises
    DeviceOwnerError (after putting the lock back) when setup could not be
    shown; `client` must already be connected."""
    turned_off = await kiosk_lock_off(hass, target)
    try:
        await device_owner.restart_meta_setup(client, target.model_key)
    except Exception:
        await restore_kiosk_lock(hass, target, turned_off or ())
        raise
    lock_note = (
        "Kiosk mode could not be turned off from Home Assistant; if the setup "
        "screen disappears, turn kiosk mode off in Kiosk Satellite."
        if turned_off is None
        else "Kiosk mode is off until setup finishes."
    )
    notify(
        hass,
        target,
        "The Portal is showing Meta's setup screen. Finish setup and the "
        f"WhatsApp login on the Portal. {lock_note} This notice updates when "
        f"the Meta login is back (checked for {int(WATCH_TIMEOUT_S // 60)} minutes).",
    )
    hass.async_create_background_task(
        _watch(hass, target, turned_off or ()),
        name=f"{DOMAIN} Meta setup watch {target.host}",
    )


async def _watch(hass: HomeAssistant, target: Target, turned_off: tuple[str, ...]) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WATCH_TIMEOUT_S
    client = AdbClient(target.host, target.port, target.key_path)
    connected = False
    adb_enabled = ""
    try:
        while True:
            try:
                if not connected:
                    await client.connect()
                    connected = True
                if await device_owner.read_meta_identity_missing(client) == ():
                    adb_enabled = (
                        await client.shell("settings get global adb_enabled")
                    ).strip()
                    break
            except Exception as err:  # noqa: BLE001 -- reconnect on the next poll
                _LOGGER.debug("Meta setup watch on %s: %s", target.name, err)
                connected = False
                with contextlib.suppress(Exception):
                    await client.close()
            if loop.time() >= deadline:
                lock = (
                    " Kiosk mode is still off; turn it back on in Kiosk Satellite once "
                    "setup is done." if turned_off else ""
                )
                notify(
                    hass,
                    target,
                    "The Meta login did not come back within "
                    f"{int(WATCH_TIMEOUT_S // 60)} minutes. Finish setup on the Portal, or "
                    f"use Configure → Enable Device Owner to show the setup screen again.{lock}",
                )
                return
            await asyncio.sleep(WATCH_INTERVAL_S)
    finally:
        with contextlib.suppress(Exception):
            await client.close()

    restored = await restore_kiosk_lock(hass, target, turned_off)
    lock = ""
    if turned_off:
        lock = (
            " Kiosk mode is back on." if restored
            else " Kiosk mode could not be turned back on; turn it on in Kiosk Satellite."
        )
    adb = "" if adb_enabled == "1" else " ADB debugging is off on the Portal."
    notify(hass, target, f"Meta setup is finished and the Meta login is back.{lock}{adb}")
