"""A device-side update failure is reported (KSM-BEHAVE-185, #183).

Kiosk Satellite records its own last install in `getUpdateStatus`. An
install started on the device, by its fleet leader, or rejected after an
on-screen tap ends there and nowhere else, and a fleet follower's update
entity may be hidden (KSM-BEHAVE-133). Read it after each health refresh
and raise one notification per device, with the fix when Meta's package
verifier was the cause.
"""
from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import ks_api_client
from .adb_client import async_probe_adb_port
from .const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_TLS_SPKI, DEFAULT_ADB_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

VERIFIER_FAILURE = "INSTALL_FAILED_VERIFICATION_FAILURE"
_POSTED_KEY = f"{DOMAIN}_update_failure_posted"
_ACTIVE_KEY = f"{DOMAIN}_update_failure_active"


def notice_id(entry_id: str) -> str:
    return f"{DOMAIN}_update_failed_{entry_id}"


def is_verifier_rejection(error: object) -> bool:
    return VERIFIER_FAILURE in str(error or "")


def verifier_action(entry: ConfigEntry, adb_open: bool) -> str:
    """KSM-BEHAVE-185/186: what the operator does about a verifier rejection."""
    if adb_open:
        return (
            f"ADB is on: press Install Kiosk Satellite on {entry.title}; KSM turns "
            "the package verifier off over ADB before installing."
        )
    return (
        f"KSM turns the package verifier off over ADB, and ADB is off on {entry.title}. "
        "Turn ADB on (USB `adb tcpip 5555`, or Meta Debug → ADB Enabled), then press "
        "Install Kiosk Satellite."
    )


def adb_target(entry: ConfigEntry) -> tuple[str, int]:
    return entry.data[CONF_HOST], entry.data.get(CONF_PORT) or DEFAULT_ADB_PORT


async def _message(entry: ConfigEntry, status: dict) -> str:
    from .ks_update import verifier_retry_allowed  # ks_update dismisses through this module

    error = status.get("lastError") or "no error text"
    message = f"The last Kiosk Satellite update on {entry.title} failed on the device: {error}"
    if is_verifier_rejection(error) and verifier_retry_allowed(entry):
        adb_open = await async_probe_adb_port(*adb_target(entry))
        message += "\n\n" + verifier_action(entry, adb_open)
    return message


@callback
def async_dismiss(hass: HomeAssistant, entry_id: str) -> None:
    hass.data.setdefault(_POSTED_KEY, {}).pop(entry_id, None)
    persistent_notification.async_dismiss(hass, notice_id(entry_id))


async def async_poll(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Read one device's update status; a failed read changes nothing."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    password = entry.data.get(CONF_PASSWORD)
    if (coordinator is None or not password or coordinator.ksm_installing
            or not coordinator.last_update_success):
        return
    active = hass.data.setdefault(_ACTIVE_KEY, set())
    if entry.entry_id in active:
        return
    active.add(entry.entry_id)
    try:
        session = async_get_clientsession(hass)
        host, pin = entry.data[CONF_HOST], entry.data.get(CONF_TLS_SPKI)
        try:
            token = await ks_api_client.login(session, host, password, pin=pin)
            response = await ks_api_client.run_command(
                session, host, token, "getUpdateStatus", pin=pin
            )
        except Exception as err:  # noqa: BLE001 -- an unreadable status reports nothing
            _LOGGER.debug("Update status unavailable on %s: %s", entry.title, err)
            return
        status = response.get("data") if isinstance(response, dict) and response.get("ok") is True else None
        if not isinstance(status, dict):
            _LOGGER.debug("Update status refused on %s", entry.title)
            return
        if not (status.get("lastError") or status.get("lastOutcome") == "failed"):
            async_dismiss(hass, entry.entry_id)
            return
        message = await _message(entry, status)
        posted = hass.data.setdefault(_POSTED_KEY, {})
        if posted.get(entry.entry_id) == message:
            return
        posted[entry.entry_id] = message
        persistent_notification.async_create(
            hass,
            message=message,
            title=f"Kiosk Satellite update failed: {entry.title}",
            notification_id=notice_id(entry.entry_id),
        )
    finally:
        active.discard(entry.entry_id)
