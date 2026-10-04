"""Kiosk Satellite self-update sequence (KSM-BEHAVE-082, #47, #70).

Updates run over Kiosk Satellite's own `:2324` API, so an install works even
when ADB is disabled after onboarding (KSM-BEHAVE-081). The single-device
Install button can fall back to ADB only before the API install begins.
Since #70 the device no longer downloads from GitHub itself: KSM uploads its
cached, signer-verified copy (KSM-BEHAVE-107/108).

Shared by the single-device Install button, per-device auto-update and the
manager's Update all -- one verified sequence, not three copies.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from pathlib import Path

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import apk_cache, ks_api_client, update_failure
from .adb_client import AdbClient, async_probe_adb_port
from .const import (
    CONF_DEVICE_PROFILE,
    CONF_HOST,
    CONF_KEY_PATH,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_TLS_SPKI,
    DOMAIN,
    SELF_UPDATE_POLL_ATTEMPTS,
    SELF_UPDATE_POLL_DELAY_S,
)
from .device_catalog import NoApprovedRecipe, require_recipe
from .helpers import target_release
from .ks_api_client import KsApiError
from .provisioning import fetch_health

_LOGGER = logging.getLogger(__name__)

OUTCOME_UPDATED = "updated"
OUTCOME_AWAITING_CONFIRMATION = "awaiting_confirmation"


def notify_awaiting_confirmation(hass: HomeAssistant, entry: ConfigEntry, outcome: str) -> None:
    """KSM-BEHAVE-181: an install waiting on the device is reported, never silent.

    Awaiting confirmation raises a per-device persistent notification; an
    updated outcome dismisses an earlier one. Any other outcome leaves it.
    """
    notice_id = f"{DOMAIN}_confirm_{entry.entry_id}"
    if outcome == OUTCOME_AWAITING_CONFIRMATION:
        persistent_notification.async_create(
            hass,
            message=(
                f"The Kiosk Satellite update was uploaded to {entry.title} but "
                "Android needs it confirmed: tap the install prompt on the "
                "device screen to finish."
            ),
            title=f"Kiosk Satellite update needs confirmation: {entry.title}",
            notification_id=notice_id,
        )
    elif outcome == OUTCOME_UPDATED:
        persistent_notification.async_dismiss(hass, notice_id)
        # KSM-BEHAVE-185: a device-side failure is over once an update lands.
        update_failure.async_dismiss(hass, entry.entry_id)


class ApiInstallUnavailable(HomeAssistantError):
    """The device API could not be reached before installation began."""


_TRANSIENT_ERRORS = (KsApiError, aiohttp.ClientError, asyncio.TimeoutError)
_UPLOAD_CHUNK = 1 << 20
_VERIFIER_FAILURE = update_failure.VERIFIER_FAILURE


def verifier_retry_allowed(entry: ConfigEntry) -> bool:
    """Only an exact, approved recipe may opt into ADB remediation."""
    model = entry.data.get(CONF_DEVICE_PROFILE)
    if not model:
        return False
    try:
        return require_recipe(model).verifier_retry_on_failure
    except NoApprovedRecipe:
        return False


async def _remediate_verifier(entry: ConfigEntry, *, already_off_ok: bool = False) -> None:
    """Record the previous device-wide value and verify the one allowed write.

    ``already_off_ok``: a verifier that already reads 0 is not an error
    (KSM-BEHAVE-186's pre-upload check, where the device's status may be stale).
    """
    client = AdbClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_KEY_PATH])
    try:
        await client.connect()
        prior = (await client.shell("settings get global package_verifier_enable")).strip()
        _LOGGER.warning("Package verifier on %s before update retry: %s", entry.title, prior)
        if already_off_ok and prior == "0":
            return
        if prior != "1":
            raise HomeAssistantError(
                f"Package verifier on {entry.title} was {prior!r}; cannot remediate"
            )
        await client.shell("settings put global package_verifier_enable 0")
        after = (await client.shell("settings get global package_verifier_enable")).strip()
        if after != "0":
            raise HomeAssistantError(
                f"Package verifier on {entry.title} did not disable (readback {after!r})"
            )
    finally:
        await client.close()


async def _clear_verifier_rejection(
    session: aiohttp.ClientSession, host: str, token: str, entry: ConfigEntry, *, pin: str | None
) -> None:
    """KSM-BEHAVE-186: before uploading, act on a verifier rejection the
    device already reported, so no prompt is raised that the verifier will
    reject after the tap. Any status read failure changes nothing."""
    try:
        status = _status_data(
            await ks_api_client.run_command(session, host, token, "getUpdateStatus", pin=pin),
            entry,
        )
    except Exception as err:  # noqa: BLE001 -- the install proceeds as before
        _LOGGER.debug("Pre-install update status unavailable on %s: %s", entry.title, err)
        return
    error = status.get("lastError")
    if not update_failure.is_verifier_rejection(error) or not verifier_retry_allowed(entry):
        return
    if not await async_probe_adb_port(*update_failure.adb_target(entry)):
        raise HomeAssistantError(
            f"Kiosk Satellite update failed on {entry.title}: the package verifier "
            f"rejected its last update ({error}). "
            + update_failure.verifier_action(entry, adb_open=False)
        )
    try:
        await _remediate_verifier(entry, already_off_ok=True)
    except Exception as adb_err:
        raise HomeAssistantError(
            f"Kiosk Satellite update failed on {entry.title}: {error}; "
            f"ADB verifier remediation failed: {adb_err}"
        ) from adb_err


async def _file_chunks(hass: HomeAssistant, path: Path) -> AsyncIterator[bytes]:
    """KSM-BEHAVE-108: stream the cached APK without blocking the loop or
    holding the whole file in memory per device."""
    handle = await hass.async_add_executor_job(path.open, "rb")
    try:
        while chunk := await hass.async_add_executor_job(handle.read, _UPLOAD_CHUNK):
            yield chunk
    finally:
        await hass.async_add_executor_job(handle.close)


def _device_abis(response: dict) -> list[str]:
    """getDeviceInfo's `data.abis`; anything unusable means "unknown" (the
    universal APK, KSM-BEHAVE-107)."""
    data = response.get("data") if isinstance(response, dict) and response.get("ok") is True else None
    abis = data.get("abis") if isinstance(data, dict) else None
    return [abi for abi in abis if isinstance(abi, str)] if isinstance(abis, list) else []


async def _async_device_abis(session, host: str, token: str, pin: str | None) -> list[str]:
    """#74: the device's ABI list, to fetch only its split. A failed or
    refused getDeviceInfo never fails the update."""
    try:
        return _device_abis(
            await ks_api_client.run_command(session, host, token, "getDeviceInfo", pin=pin)
        )
    except Exception as err:  # noqa: BLE001 -- unknown ABIs fall back to universal
        _LOGGER.debug("getDeviceInfo failed on %s; using the universal APK: %s", host, err)
        return []


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


async def async_self_update_entry(
    hass: HomeAssistant, entry: ConfigEntry, *, reinstall: bool = False
) -> str:
    """KSM-BEHAVE-082: update Kiosk Satellite to the shared release check's
    latest version (or the version pinned in global settings, KSM-BEHAVE-114)
    over its own API.

    Returns OUTCOME_UPDATED or OUTCOME_AWAITING_CONFIRMATION. Raises
    HomeAssistantError for the "failed" outcome. Only the explicit install
    button sets ``reinstall``; it requests installation even for the same build.
    The verifier recovery for an approved Portal recipe may use ADB, after a
    rejection in this run or one the device reported before it (KSM-BEHAVE-186).
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and coordinator.ksm_installing:
        raise HomeAssistantError(
            f"Kiosk Satellite install already in progress on {entry.title}"
        )

    release_info = target_release(hass)
    if release_info is None:
        raise HomeAssistantError("No usable Kiosk Satellite release is known")
    version = release_info.version

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
            try:
                token = await ks_api_client.login(session, host, password, pin=pin)
            except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as err:
                # A certificate or TLS failure is a trust failure, not an
                # unavailable API. Never bypass it by crossing to ADB.
                if isinstance(err, (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError)):
                    raise
                raise ApiInstallUnavailable(
                    f"Kiosk Satellite API is unreachable on {entry.title}"
                ) from err
            await _clear_verifier_rejection(session, host, token, entry, pin=pin)
            abis = await _async_device_abis(session, host, token, pin)
            try:
                apk = await apk_cache.async_release_apk(hass, release_info, abis)
            except Exception as err:  # no asset, download, signer pin
                raise HomeAssistantError(
                    f"Kiosk Satellite {version} APK unavailable for {entry.title}: {err}"
                ) from err
            size = await hass.async_add_executor_job(lambda: apk.stat().st_size)
            for attempt in range(2):
                try:
                    _LOGGER.info("Uploading %s to %s", apk.name, entry.title)
                    uploaded = await ks_api_client.upload_update(
                        session, host, token, _file_chunks(hass, apk), size, pin=pin
                    )
                    if uploaded.get("ok") is not True:
                        raise HomeAssistantError(
                            f"Kiosk Satellite update failed on {entry.title}: "
                            f"{uploaded.get('error') or 'upload refused'}"
                        )
                    data = uploaded.get("data") or {}
                    same_build = (data.get("buildNumber") is not None
                                  and data.get("buildNumber") == data.get("currentBuild"))
                    if same_build and not reinstall:
                        if coordinator is not None:
                            await coordinator.async_request_refresh()
                        return OUTCOME_UPDATED
                    result = await ks_api_client.run_command(
                        session, host, token, "installUploadedApk", pin=pin
                    )
                    if not result.get("ok", False):
                        raise HomeAssistantError(
                            f"Kiosk Satellite update failed on {entry.title}: "
                            f"{result.get('error') or 'installUploadedApk rejected'}"
                        )
                    outcome = await _poll_until_resolved(
                        session, host, token, version, entry, pin=pin,
                        same_build_reinstall=same_build and reinstall,
                    )
                    if coordinator is not None:
                        await coordinator.async_request_refresh()
                    return outcome
                except HomeAssistantError as err:
                    if attempt or _VERIFIER_FAILURE not in str(err) or not verifier_retry_allowed(entry):
                        raise
                    try:
                        await _remediate_verifier(entry)
                    except Exception as adb_err:
                        raise HomeAssistantError(
                            f"{err}; ADB verifier remediation failed: {adb_err}"
                        ) from adb_err
        except _TRANSIENT_ERRORS as err:
            raise HomeAssistantError(
                f"Kiosk Satellite update failed on {entry.title}: {err}"
            ) from err
    finally:
        if coordinator is not None:
            coordinator.ksm_installing = False
            coordinator.async_update_listeners()
        # KSM-BEHAVE-109: this device's version may have changed.
        await apk_cache.async_prune(hass)


async def _poll_until_resolved(
    session: aiohttp.ClientSession,
    host: str,
    token: str,
    version: str,
    entry: ConfigEntry,
    *,
    pin: str | None,
    same_build_reinstall: bool = False,
) -> str:
    """Poll /api/health and getUpdateStatus for a bounded window. A
    connection refused while the app restarts is expected, not a failure."""
    last_outcome: str | None = None
    for attempt in range(SELF_UPDATE_POLL_ATTEMPTS):
        try:
            health = await fetch_health(session, host, pin=pin)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            health = None
        health_matches = health is not None and health.get("appVersion") == version
        if health_matches and not same_build_reinstall:
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
            if (same_build_reinstall and health_matches and last_outcome == "silent"
                    and status.get("installing") is False):
                return OUTCOME_UPDATED

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
    from . import fleet

    checks = [
        (entry, coordinator)
        for entry_id, coordinator in list(hass.data.get(DOMAIN, {}).items())
        if (entry := fleet.resolve_device(hass, entry_id)) is not None
    ]
    outcomes = await asyncio.gather(
        *(async_check_device_for_update(hass, entry, coordinator) for entry, coordinator in checks)
    )
    results = {entry.title: outcome for (entry, _), outcome in zip(checks, outcomes)}
    _LOGGER.info("Kiosk Satellite update check on devices: %s", results)
    return results


async def async_check_device_for_update(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> str:
    """Ask one loaded device to refresh its own update state (KSM-BEHAVE-117)."""
    password = entry.data.get(CONF_PASSWORD)
    if not password:
        return "skipped: no password stored"
    if coordinator is not None and coordinator.ksm_installing:
        return "skipped: install in progress"
    session = async_get_clientsession(hass)
    host = entry.data[CONF_HOST]
    pin = entry.data.get(CONF_TLS_SPKI)
    try:
        token = await ks_api_client.login(session, host, password, pin=pin)
        result = await ks_api_client.run_command(
            session, host, token, "checkUpdateNow", pin=pin
        )
    except Exception as err:
        _LOGGER.warning("Kiosk Satellite update check failed on %s: %s", entry.title, err)
        return f"failed: {err}"
    if not isinstance(result, dict) or result.get("ok") is not True:
        error = result.get("error") if isinstance(result, dict) else None
        _LOGGER.warning(
            "Kiosk Satellite update check failed on %s: %s",
            entry.title, error or "checkUpdateNow rejected",
        )
        return f"failed: {error or 'checkUpdateNow rejected'}"
    return f"sees {(result.get('data') or {}).get('availableVersion')}"
