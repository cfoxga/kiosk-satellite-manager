"""Add a Portal hostname to HA's Let's Encrypt add-on (KSM-BEHAVE-182, #178).

KSM performs no ACME itself: it appends the name to the add-on's `domains`
through the Supervisor, runs the add-on, and re-reads `/ssl`. The add-on's
options carry the DNS credential, so they pass through memory only and are
never logged or put in an error message.
"""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio

from . import le_certificate
from .const import DOMAIN
from .le_certificate import CertificateMaterial, CertificateUnavailable

_LOGGER = logging.getLogger(__name__)

ADDON_SLUG = "core_letsencrypt"
POLL_S = 5
RUN_TIMEOUT_S = 600
_LOCK_KEY = f"{DOMAIN}_le_addon_lock"
_RUNNING = {"startup", "started"}


def _manual(hostname: str) -> str:
    return (
        f"Add {hostname} to the Let's Encrypt add-on's domains, run the add-on, "
        "then choose this certificate again"
    )


def _addons(hass: HomeAssistant):
    """Supervisor add-on client, or CertificateUnavailable without a Supervisor."""
    if not is_hassio(hass):
        raise CertificateUnavailable("Home Assistant Supervisor is unavailable")
    from homeassistant.components.hassio import get_supervisor_client  # noqa: PLC0415

    return get_supervisor_client(hass).addons


def _state(info) -> str:
    return str(getattr(info.state, "value", info.state))


async def _write_options(addons, options: dict) -> None:
    from aiohasupervisor.models import AddonsOptions  # noqa: PLC0415

    await addons.set_addon_options(ADDON_SLUG, AddonsOptions(config=options))


async def async_add_hostname(hass: HomeAssistant, hostname: str) -> CertificateMaterial:
    """Have the add-on issue a certificate covering `hostname` and return it.

    Raises CertificateUnavailable when it can't; the add-on's previous `domains`
    are then restored, so one unissuable name never breaks every renewal.
    """
    lock = hass.data.setdefault(_LOCK_KEY, asyncio.Lock())
    async with lock:
        try:
            addons = _addons(hass)
            info = await addons.addon_info(ADDON_SLUG)
        except CertificateUnavailable as err:
            raise CertificateUnavailable(f"{err}. {_manual(hostname)}") from None
        except Exception:  # noqa: BLE001 - Supervisor errors may echo option values
            raise CertificateUnavailable(
                f"The Let's Encrypt add-on is not installed or not reachable. {_manual(hostname)}"
            ) from None
        previous = dict(info.options or {})
        domains = list(previous.get("domains") or [])
        appended = hostname not in domains
        try:
            if appended:
                await _write_options(addons, {**previous, "domains": [*domains, hostname]})
                _LOGGER.info("Added %s to the Let's Encrypt add-on domains", hostname)
            await addons.start_addon(ADDON_SLUG)
            state = await _async_wait_stopped(addons)
            material = None
            if state not in ("error", "timeout"):
                try:
                    material = await hass.async_add_executor_job(
                        le_certificate.load_for_hostname, hostname
                    )
                except CertificateUnavailable:
                    material = None
        except Exception:  # noqa: BLE001
            material = None
            _LOGGER.warning("Let's Encrypt add-on run for %s failed", hostname)
        if material is not None:
            return material
        if appended:
            try:
                await _write_options(addons, previous)
                _LOGGER.info("Removed %s from the Let's Encrypt add-on domains again", hostname)
            except Exception:  # noqa: BLE001
                _LOGGER.error(
                    "Could not restore the Let's Encrypt add-on domains after %s failed", hostname
                )
        raise CertificateUnavailable(
            f"The Let's Encrypt add-on did not issue a certificate for {hostname}; "
            "check the add-on's log"
        )


async def _async_wait_stopped(addons) -> str:
    """Poll until the one-shot add-on run ends; returns its final state."""
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while True:
        state = _state(await addons.addon_info(ADDON_SLUG))
        if state not in _RUNNING:
            return state
        if time.monotonic() >= deadline:
            return "timeout"
        await asyncio.sleep(POLL_S)
