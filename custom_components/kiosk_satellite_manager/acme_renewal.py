"""Install, renew and migrate KSM-issued device certificates (#200).

KSM-BEHAVE-205 install, -206 renewal, -207 migration off the shared add-on
certificate, -209 one key per device. Devices are handled one at a time.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import acme_issuer, dns_cloudflare, fleet, ks_api_client, ks_tls, le_certificate, le_certificate_sync
from .const import (
    ACME_DEVICE_FIELDS,
    CONF_ACME_ACCOUNT_KEY,
    CONF_ACME_ACCOUNT_URL,
    CONF_ACME_CERTIFICATE,
    CONF_ACME_DNS_PROVIDER,
    CONF_ACME_DNS_TOKEN,
    CONF_ACME_EMAIL,
    CONF_ACME_EXPIRES,
    CONF_ACME_FINGERPRINT,
    CONF_ACME_HOSTNAME,
    CONF_ACME_PENDING,
    CONF_ACME_PRIVATE_KEY,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_LE_CERTIFICATE_FINGERPRINT,
    CONF_LE_CERTIFICATE_HOSTNAME,
    CONF_PASSWORD,
    CONF_TLS_SPKI,
    DNS_PROVIDER_CLOUDFLARE,
    DOMAIN,
    ENTRY_TYPE_MANAGER,
)
from .device_repairs import acme_renewal_issue_id, le_certificate_sync_issue_id, tls_issue_id
from .ks_api_client import KsApiError

_LOGGER = logging.getLogger(__name__)
_LOCK_KEY = f"{DOMAIN}_acme_renewal_lock"
SETUP_ISSUE_ID = "acme_setup_required"
RENEW_BEFORE = timedelta(days=30)
REPAIR_BEFORE = timedelta(days=21)
CHECK_INTERVAL = timedelta(hours=6)
_FAILURES = (le_certificate.CertificateUnavailable, KsApiError, aiohttp.ClientError,
             TimeoutError, ValueError, KeyError)

Device = ConfigEntry | fleet.DeviceEntry


def manager_entry(hass: HomeAssistant) -> ConfigEntry | None:
    return next((e for e in hass.config_entries.async_entries(DOMAIN)
                 if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER), None)


def acme_settings(hass: HomeAssistant) -> dict[str, Any] | None:
    """The manager's Certificates settings, or None when not usable."""
    entry = manager_entry(hass)
    if entry is None:
        return None
    data = entry.data
    if not data.get(CONF_ACME_DNS_TOKEN) or not data.get(CONF_ACME_ACCOUNT_KEY):
        return None
    return dict(data)


def all_devices(hass: HomeAssistant) -> list[Device]:
    """Every physical device once: plain per-device entries and subentries."""
    plain = [e for e in hass.config_entries.async_entries(DOMAIN) if not e.data.get(CONF_ENTRY_TYPE)]
    return [*plain, *fleet.device_entries(hass)]


def hostname_in_use(hass: HomeAssistant, hostname: str, except_id: str) -> bool:
    """KSM-BEHAVE-209: another managed device already holds this name."""
    hostname = hostname.strip().rstrip(".").lower()
    return any(
        device.entry_id != except_id and hostname in (
            str(device.data.get(CONF_ACME_HOSTNAME) or "").lower(),
            str(device.data.get(CONF_LE_CERTIFICATE_HOSTNAME) or "").lower(),
        )
        for device in all_devices(hass)
    )


def is_legacy(device: Device) -> bool:
    return bool(device.data.get(CONF_LE_CERTIFICATE_HOSTNAME)) and not device.data.get(CONF_ACME_HOSTNAME)


def _expires(device: Device) -> datetime | None:
    try:
        return datetime.fromisoformat(device.data[CONF_ACME_EXPIRES])
    except (KeyError, TypeError, ValueError):
        return None


def lock(hass: HomeAssistant) -> asyncio.Lock:
    """Serializes every certificate install and renewal on this HA instance."""
    return hass.data.setdefault(_LOCK_KEY, asyncio.Lock())


def _pending(device: Device, hostname: str) -> le_certificate.CertificateMaterial | None:
    """The saved, not yet installed certificate for `hostname`, while it is
    still good for longer than the renewal window."""
    saved = device.data.get(CONF_ACME_PENDING)
    if not isinstance(saved, dict) or saved.get("hostname") != hostname:
        return None
    try:
        material = le_certificate.validate(
            str(saved["certificate"]).encode("ascii"), str(saved["private_key"]).encode("ascii"),
            hostname, exact=True)
    except (le_certificate.CertificateUnavailable, KeyError, UnicodeEncodeError):
        return None
    if material.not_after is None or material.not_after - datetime.now(timezone.utc) < RENEW_BEFORE:
        return None
    return material


async def async_obtain(
    hass: HomeAssistant, settings: dict[str, Any], device: Device, hostname: str,
    key_pem: str | None,
) -> le_certificate.CertificateMaterial:
    """The device's saved pending certificate for `hostname`, else a new one.

    Reusing the pending one keeps a failed install from ordering again (Let's
    Encrypt allows five identical certificates a week)."""
    pending = _pending(device, hostname)
    if pending is not None:
        return pending
    return await acme_issuer.async_issue(hass, settings, hostname, key_pem)


async def async_install(
    hass: HomeAssistant, device: Device, hostname: str,
    material: le_certificate.CertificateMaterial, pin: str,
) -> None:
    """Put `material` on the device over `pin` and store it (KSM-BEHAVE-205/206).
    Call it holding `lock(hass)`.

    The material is saved as pending first, so a device that ends up serving
    it after a failed check is adopted on the next attempt; a device already
    serving it is adopted without importing again (KSM-BEHAVE-180)."""
    data = device.data
    if data.get(CONF_ACME_PENDING, {}).get("certificate") != material.certificate:
        fleet.update_device(hass, device, data={**data, CONF_ACME_PENDING: {
            "hostname": hostname, "certificate": material.certificate,
            "private_key": material.private_key,
        }})
        data = device.data
    session = async_get_clientsession(hass)
    served = await ks_api_client.probe_https_identity(session, data[CONF_HOST])
    if served == (material.spki_sha256, material.fingerprint):
        new_pin = material.spki_sha256
    else:
        new_pin = await ks_tls.async_import_certificate(
            session, data[CONF_HOST], data[CONF_PASSWORD], pin, material)
    stored = {k: v for k, v in device.data.items() if k not in (
        CONF_LE_CERTIFICATE_HOSTNAME, CONF_LE_CERTIFICATE_FINGERPRINT, CONF_ACME_PENDING)}
    stored.update({
        CONF_TLS_SPKI: new_pin,
        CONF_ACME_HOSTNAME: hostname,
        CONF_ACME_CERTIFICATE: material.certificate,
        CONF_ACME_PRIVATE_KEY: material.private_key,
        CONF_ACME_FINGERPRINT: material.fingerprint,
        CONF_ACME_EXPIRES: material.not_after.isoformat() if material.not_after else "",
    })
    fleet.update_device(hass, device, data=stored)
    for issue_id in (acme_renewal_issue_id(device.entry_id), tls_issue_id(device.entry_id),
                     le_certificate_sync_issue_id(device.entry_id)):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    coordinator = hass.data.get(DOMAIN, {}).get(device.entry_id)
    if coordinator is not None:
        await coordinator.async_request_refresh()


def strip_certificate(data: dict[str, Any]) -> dict[str, Any]:
    """KSM-BEHAVE-208: device data without any certificate fields."""
    return {k: v for k, v in data.items() if k not in (
        *ACME_DEVICE_FIELDS, CONF_LE_CERTIFICATE_HOSTNAME, CONF_LE_CERTIFICATE_FINGERPRINT)}


async def async_check_device(hass: HomeAssistant, device: Device, *, force: bool = False) -> bool:
    """Renew a KSM certificate nearing expiry, or migrate a legacy device.

    True when a new certificate was installed. Failures keep the stored
    certificate, key and pin; the next tick retries."""
    async with lock(hass):
        return await _check_device(hass, device, force=force)


async def _check_device(hass: HomeAssistant, device: Device, *, force: bool) -> bool:
    data = device.data
    settings = acme_settings(hass)
    if data.get(CONF_ACME_HOSTNAME):
        if not data.get(CONF_TLS_SPKI):
            return False
        expires = _expires(device)
        now = datetime.now(timezone.utc)
        if not force and expires is not None and expires - now >= RENEW_BEFORE:
            return False
        try:
            if settings is None:
                raise le_certificate.CertificateUnavailable("Certificates settings are not configured")
            # KSM-BEHAVE-206: the stored key, so the pin does not change.
            material = await async_obtain(
                hass, settings, device, data[CONF_ACME_HOSTNAME], data[CONF_ACME_PRIVATE_KEY])
            await async_install(hass, device, data[CONF_ACME_HOSTNAME], material, data[CONF_TLS_SPKI])
        except _FAILURES as err:
            _LOGGER.warning("Certificate renewal failed for %s: %s", device.title, err)
            if force or expires is None or expires - now < REPAIR_BEFORE:
                ir.async_create_issue(
                    hass, DOMAIN, acme_renewal_issue_id(device.entry_id),
                    is_fixable=True, severity=ir.IssueSeverity.ERROR,
                    translation_key="acme_renewal_failed",
                    translation_placeholders={"name": device.title,
                                              "hostname": data[CONF_ACME_HOSTNAME]},
                    data={"entry_id": device.entry_id},
                )
            return False
        return True
    if not is_legacy(device) or settings is None or not data.get(CONF_TLS_SPKI):
        return False
    hostname = data[CONF_LE_CERTIFICATE_HOSTNAME]
    try:
        # KSM-BEHAVE-207/209: a new key, never the shared `/ssl` one.
        material = await async_obtain(hass, settings, device, hostname, None)
        await async_install(hass, device, hostname, material, data[CONF_TLS_SPKI])
    except _FAILURES as err:
        _LOGGER.warning("Moving %s to its own certificate failed: %s", device.title, err)
        return False
    return True


async def _async_legacy_sync(hass: HomeAssistant, device: Device) -> None:
    """KSM-BEHAVE-207: the retained `/ssl` sync, for legacy devices only."""
    try:
        await le_certificate_sync.async_sync_device(hass, device)
    except _FAILURES as err:
        _LOGGER.warning("HA certificate sync failed for %s: %s", device.title, err)
        ir.async_create_issue(
            hass, DOMAIN, le_certificate_sync_issue_id(device.entry_id),
            is_fixable=False, severity=ir.IssueSeverity.ERROR,
            translation_key="le_certificate_sync_failed",
            translation_placeholders={"name": device.title, "host": device.data.get(CONF_HOST, "")},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, le_certificate_sync_issue_id(device.entry_id))


async def async_device_tick(hass: HomeAssistant, device: Device) -> None:
    """One device's six-hourly certificate work."""
    if device.data.get(CONF_ACME_HOSTNAME):
        await async_check_device(hass, device)
    elif is_legacy(device):
        # Until a migration lands, the shared certificate keeps renewing.
        if not (acme_settings(hass) and await async_check_device(hass, device)):
            await _async_legacy_sync(hass, device)
    sync_setup_repair(hass)


async def async_tick(hass: HomeAssistant, _now: Any = None) -> None:
    """Every loaded device in turn (KSM-BEHAVE-206/207)."""
    loaded = hass.data.get(DOMAIN, {})
    for device in all_devices(hass):
        if device.entry_id in loaded and getattr(device, "present", True):
            try:
                await async_device_tick(hass, device)
            except Exception as err:  # noqa: BLE001 -- one device never stops the rest
                _LOGGER.warning("Certificate check failed for %s: %s", device.title,
                                type(err).__name__)
    sync_setup_repair(hass)


def sync_setup_repair(hass: HomeAssistant) -> None:
    """One manager repair while legacy devices wait for Certificates settings."""
    if acme_settings(hass) is None and any(is_legacy(d) for d in all_devices(hass)):
        ir.async_create_issue(
            hass, DOMAIN, SETUP_ISSUE_ID, is_fixable=True,
            severity=ir.IssueSeverity.WARNING, translation_key=SETUP_ISSUE_ID,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, SETUP_ISSUE_ID)


_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+")
CONF_ACME_REMOVE = "acme_remove"


def settings_schema(saved: dict[str, Any]) -> vol.Schema:
    """The Certificates form (KSM-BEHAVE-203); the token is never echoed back."""
    return vol.Schema({
        vol.Required(CONF_ACME_EMAIL, default=saved.get(CONF_ACME_EMAIL, "")): str,
        vol.Required(CONF_ACME_DNS_PROVIDER, default=DNS_PROVIDER_CLOUDFLARE):
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=[selector.SelectOptionDict(value=DNS_PROVIDER_CLOUDFLARE, label="Cloudflare")],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )),
        vol.Optional(CONF_ACME_DNS_TOKEN): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        vol.Optional(CONF_ACME_REMOVE, default=False): bool,
    })


async def async_save_settings(
    hass: HomeAssistant, entry: ConfigEntry, user_input: dict[str, Any]
) -> dict[str, str]:
    """Validate and store Certificates settings; returns form errors.

    Nothing is stored unless every check passes. A blank token keeps the
    saved one; Remove deletes only the token."""
    saved = dict(entry.data)
    if user_input.get(CONF_ACME_REMOVE):
        saved.pop(CONF_ACME_DNS_TOKEN, None)
        hass.config_entries.async_update_entry(entry, data=saved)
        sync_setup_repair(hass)
        return {}
    email = str(user_input.get(CONF_ACME_EMAIL, "")).strip()
    if not _EMAIL.fullmatch(email):
        return {CONF_ACME_EMAIL: "invalid_email"}
    token = str(user_input.get(CONF_ACME_DNS_TOKEN) or saved.get(CONF_ACME_DNS_TOKEN) or "").strip()
    session = async_get_clientsession(hass)
    try:
        await dns_cloudflare.async_verify_token(session, token)
        zones = await dns_cloudflare.async_zones(session, token)
    except dns_cloudflare.CloudflareError as err:
        if err.code == "cannot_connect":
            return {"base": "cannot_connect"}
        return {CONF_ACME_DNS_TOKEN: "invalid_token"}
    if not zones:
        return {CONF_ACME_DNS_TOKEN: "no_zones"}
    account_key = saved.get(CONF_ACME_ACCOUNT_KEY)
    account_url = saved.get(CONF_ACME_ACCOUNT_URL)
    if not account_key or not account_url:
        try:
            account_key, account_url = await acme_issuer.async_register(hass, email)
        except le_certificate.CertificateUnavailable:
            return {"base": "acme_registration_failed"}
    hass.config_entries.async_update_entry(entry, data={
        **saved, CONF_ACME_EMAIL: email, CONF_ACME_DNS_PROVIDER: DNS_PROVIDER_CLOUDFLARE,
        CONF_ACME_DNS_TOKEN: token, CONF_ACME_ACCOUNT_KEY: account_key,
        CONF_ACME_ACCOUNT_URL: account_url,
    })
    sync_setup_repair(hass)
    # KSM-BEHAVE-207: legacy devices move to their own certificates now.
    hass.async_create_background_task(async_tick(hass), f"{DOMAIN}_acme_migration")
    return {}
