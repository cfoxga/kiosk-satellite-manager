"""Kiosk Satellite self-update sequence (KSM-BEHAVE-082, #47).

Updates run over Kiosk Satellite's own `:2324` API -- the same mechanism
ESPHome's Kiosk Satellite update entity uses -- so an install works even
when ADB is disabled after onboarding (KSM-BEHAVE-081). ADB stays only
behind the explicit Install/Reinstall and Uninstall buttons in button.py.

Shared by the update entity's Install, per-device auto-update and the
manager's Update all -- one verified sequence, not three copies.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import ks_api_client
from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_TLS_SPKI,
    DOMAIN,
    RELEASE_COORDINATOR_KEY,
    SELF_UPDATE_POLL_ATTEMPTS,
    SELF_UPDATE_POLL_DELAY_S,
)
from .ks_api_client import KsApiError
from .provisioning import fetch_health

_LOGGER = logging.getLogger(__name__)

OUTCOME_UPDATED = "updated"
OUTCOME_AWAITING_CONFIRMATION = "awaiting_confirmation"

_TRANSIENT_ERRORS = (KsApiError, aiohttp.ClientError, asyncio.TimeoutError)


def _status_data(response: dict, entry: ConfigEntry) -> dict:
    """Read status fields from Kiosk Satellite's command response envelope."""
    if (
        not isinstance(response, dict)
        or response.get("ok") is not True
        or not isinstance(response.get("data"), dict)
    ):
        raise HomeAssistantError(
            f"Kiosk Satellite update status unavailable on {entry.title}"
        )
    return response["data"]


async def async_self_update_entry(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """KSM-BEHAVE-082: update Kiosk Satellite to the shared release check's
    latest version over its own API.

    Returns OUTCOME_UPDATED or OUTCOME_AWAITING_CONFIRMATION. Raises
    HomeAssistantError for the "not offered" and "failed" outcomes -- never
    constructs an AdbClient.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and coordinator.ksm_installing:
        raise HomeAssistantError(
            f"Kiosk Satellite install already in progress on {entry.title}"
        )

    release = hass.data.get(RELEASE_COORDINATOR_KEY)
    if release is None or release.data is None:
        raise HomeAssistantError("No usable Kiosk Satellite release is known")
    version = release.data.version

    host = entry.data[CONF_HOST]
    password = entry.data.get(CONF_PASSWORD)
    pin = entry.data.get(CONF_TLS_SPKI)
    if not password:
        raise HomeAssistantError(
            f"no Kiosk Satellite password stored for {entry.title}"
        )
    session = async_get_clientsession(hass)

    if coordinator is not None:
        coordinator.ksm_installing = True
        coordinator.async_update_listeners()
    try:
        try:
            token = await ks_api_client.login(session, host, password, pin=pin)
            await ks_api_client.run_command(session, host, token, "checkUpdateNow", pin=pin)
            status = _status_data(
                await ks_api_client.run_command(session, host, token, "getUpdateStatus", pin=pin),
                entry,
            )
        except _TRANSIENT_ERRORS as err:
            raise HomeAssistantError(
                f"Kiosk Satellite update failed on {entry.title}: {err}"
            ) from err

        if status.get("availableVersion") != version:
            raise HomeAssistantError(
                f"Kiosk Satellite on {entry.title} does not yet see {version}"
            )

        try:
            # Diagnostics only (nativeSilent/helper/shizukuReady) -- not
            # used to gate the install, just logged for support.
            installer_status = await ks_api_client.run_command(
                session, host, token, "getUpdateInstallerStatus", pin=pin
            )
            _LOGGER.debug(
                "Kiosk Satellite update installer status on %s: %s",
                entry.title,
                installer_status,
            )
            result = await ks_api_client.run_command(session, host, token, "installUpdate", pin=pin)
        except _TRANSIENT_ERRORS as err:
            raise HomeAssistantError(
                f"Kiosk Satellite update failed on {entry.title}: {err}"
            ) from err

        if not result.get("ok", False):
            raise HomeAssistantError(
                f"Kiosk Satellite update failed on {entry.title}: "
                f"{result.get('error') or 'installUpdate rejected'}"
            )

        outcome = await _poll_until_resolved(session, host, token, version, entry, pin=pin)
        if coordinator is not None:
            # The poll above confirmed the new version over its own direct
            # /api/health call -- refresh the entry's health coordinator too
            # so installed_version reflects it immediately, not after the
            # next scheduled poll.
            await coordinator.async_request_refresh()
        return outcome
    finally:
        if coordinator is not None:
            coordinator.ksm_installing = False
            coordinator.async_update_listeners()


async def _poll_until_resolved(
    session: aiohttp.ClientSession,
    host: str,
    token: str,
    version: str,
    entry: ConfigEntry,
    *,
    pin: str | None,
) -> str:
    """Poll /api/health and getUpdateStatus for a bounded window. A
    connection refused while the app restarts is expected, not a failure."""
    last_outcome: str | None = None
    for attempt in range(SELF_UPDATE_POLL_ATTEMPTS):
        try:
            health = await fetch_health(session, host, pin=pin)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            health = None
        if health is not None and health.get("appVersion") == version:
            return OUTCOME_UPDATED

        try:
            status = _status_data(
                await ks_api_client.run_command(session, host, token, "getUpdateStatus", pin=pin),
                entry,
            )
        except _TRANSIENT_ERRORS:
            status = None
        if status is not None:
            if status.get("lastError"):
                raise HomeAssistantError(
                    f"Kiosk Satellite update failed on {entry.title}: {status['lastError']}"
                )
            last_outcome = status.get("lastOutcome")

        if attempt < SELF_UPDATE_POLL_ATTEMPTS - 1:
            await asyncio.sleep(SELF_UPDATE_POLL_DELAY_S)

    if last_outcome == "confirm":
        return OUTCOME_AWAITING_CONFIRMATION
    raise HomeAssistantError(
        f"Kiosk Satellite update failed on {entry.title}: update did not complete"
    )


async def async_check_devices_for_update(hass: HomeAssistant) -> dict[str, str]:
    """KSM-BEHAVE-103: ask every loaded device's Kiosk Satellite to check
    GitHub now (`checkUpdateNow`), so a release the manager just saw reaches
    each device without waiting for its own 12-hour check.

    Concurrent, API-only (KSM-BEHAVE-081), installs nothing. Returns an
    outcome per device title; a failure is logged and never stops the rest.
    """
    session = async_get_clientsession(hass)

    async def _check(entry: ConfigEntry, coordinator) -> str:
        password = entry.data.get(CONF_PASSWORD)
        if not password:
            return "skipped: no password stored"
        if coordinator is not None and coordinator.ksm_installing:
            # The running install already sent checkUpdateNow (KSM-BEHAVE-082).
            return "skipped: install in progress"
        host = entry.data[CONF_HOST]
        pin = entry.data.get(CONF_TLS_SPKI)
        try:
            token = await ks_api_client.login(session, host, password, pin=pin)
            result = await ks_api_client.run_command(
                session, host, token, "checkUpdateNow", pin=pin
            )
        except Exception as err:  # continue with the next device
            _LOGGER.warning(
                "Kiosk Satellite update check failed on %s: %s", entry.title, err
            )
            return f"failed: {err}"
        if not isinstance(result, dict) or result.get("ok") is not True:
            error = result.get("error") if isinstance(result, dict) else None
            _LOGGER.warning(
                "Kiosk Satellite update check failed on %s: %s",
                entry.title, error or "checkUpdateNow rejected",
            )
            return f"failed: {error or 'checkUpdateNow rejected'}"
        return f"sees {(result.get('data') or {}).get('availableVersion')}"

    checks = [
        (entry, coordinator)
        for entry_id, coordinator in list(hass.data.get(DOMAIN, {}).items())
        if (entry := hass.config_entries.async_get_entry(entry_id)) is not None
    ]
    outcomes = await asyncio.gather(*(_check(entry, coordinator) for entry, coordinator in checks))
    results = {entry.title: outcome for (entry, _), outcome in zip(checks, outcomes)}
    _LOGGER.info("Kiosk Satellite update check on devices: %s", results)
    return results
