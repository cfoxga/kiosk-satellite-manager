"""Voice Satellite entry management (KSM-BEHAVE-100/101, #65).

Each KS device needs a `voice_satellite` config entry (HACS
jxlarrea/voice-satellite-card-integration) and KS's per-device
`ha.satellite_entity` pointed at that entry's assist_satellite entity.

Accepted risk: Voice Satellite has no public setup hook, so KSM drives its
one-field user flow and mirrors its unique ID (the name, lowercased, spaces
to `_`). Every failure here is logged and retried on the next setup; it
never fails the device entry.
"""
from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import IntegrationNotFound, async_get_integration

from . import ks_api_client
from .const import CONF_ENTRY_TYPE, CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_TLS_SPKI, DOMAIN
from .ks_api_client import KsApiError

_LOGGER = logging.getLogger(__name__)

VS_DOMAIN = "voice_satellite"
VS_REPOSITORY = "jxlarrea/voice-satellite-card-integration"
HACS_DOMAIN = "hacs"
ISSUE_VOICE_SATELLITE_MISSING = "voice_satellite_missing"
SATELLITE_SETTING = "ha.satellite_entity"


def satellite_unique_id(name: str) -> str:
    """Voice Satellite's own unique ID for a satellite name."""
    return name.strip().lower().replace(" ", "_")


async def async_voice_satellite_installed(hass: HomeAssistant) -> bool:
    try:
        await async_get_integration(hass, VS_DOMAIN)
    except IntegrationNotFound:
        return False
    return True


async def _async_satellite_entry(hass: HomeAssistant, name: str) -> ConfigEntry | None:
    unique_id = satellite_unique_id(name)
    entry = hass.config_entries.async_entry_for_domain_unique_id(VS_DOMAIN, unique_id)
    if entry is not None:
        return entry
    result = await hass.config_entries.flow.async_init(
        VS_DOMAIN, context={"source": SOURCE_USER}, data={"name": name}
    )
    if result["type"] is FlowResultType.CREATE_ENTRY:
        return result["result"]
    # An abort (e.g. a racing duplicate) still leaves the entry findable.
    return hass.config_entries.async_entry_for_domain_unique_id(VS_DOMAIN, unique_id)


def _satellite_entity(hass: HomeAssistant, vs_entry: ConfigEntry) -> str | None:
    for ent in er.async_entries_for_config_entry(er.async_get(hass), vs_entry.entry_id):
        if ent.domain == "assist_satellite":
            return ent.entity_id
    return None


def _named_for_another_device(
    hass: HomeAssistant, entry: ConfigEntry, satellite: er.RegistryEntry
) -> bool:
    """True when the satellite's Voice Satellite entry carries another KSM
    device's name -- e.g. a device renamed away from "Great Room Kiosk"
    while a new device took that name. The renamed device lets it go."""
    vs_entry = hass.config_entries.async_get_entry(satellite.config_entry_id or "")
    if vs_entry is None:
        return False
    return any(
        other.entry_id != entry.entry_id
        and other.data.get(CONF_ENTRY_TYPE) is None
        and satellite_unique_id(other.data.get(CONF_NAME) or other.title) == vs_entry.unique_id
        for other in hass.config_entries.async_entries(DOMAIN)
    )


async def async_bind_voice_satellite(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """KSM-BEHAVE-100: keep the device's live Voice Satellite binding, or
    create/adopt its entry by name and point KS at that entry's satellite
    entity. Returns the bound entity id, or None."""
    if not await async_voice_satellite_installed(hass):
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_VOICE_SATELLITE_MISSING,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_VOICE_SATELLITE_MISSING,
            data={"issue": ISSUE_VOICE_SATELLITE_MISSING},
        )
        return None
    ir.async_delete_issue(hass, DOMAIN, ISSUE_VOICE_SATELLITE_MISSING)

    host = entry.data[CONF_HOST]
    name = entry.data.get(CONF_NAME) or entry.title
    password = entry.data.get(CONF_PASSWORD)
    if not password:
        _LOGGER.warning("No Kiosk Satellite password stored for %s; Voice Satellite not managed", name)
        return None
    pin = entry.data.get(CONF_TLS_SPKI)
    session = async_get_clientsession(hass)
    try:
        token = await ks_api_client.login(session, host, password, pin=pin)
        current = (await ks_api_client.get_settings(session, host, token, pin=pin)).get(SATELLITE_SETTING)
    except (KsApiError, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Could not read the Voice Satellite binding of %s: %s", name, err)
        return None
    # A device already bound to a live Voice Satellite entity keeps it, even
    # when that satellite's name differs from the device's (e.g. a KSM entry
    # "Theater GTV" on a hand-made "Theater Google TV" satellite) -- unless
    # another KSM device now has that satellite's name.
    existing = er.async_get(hass).async_get(current) if current else None
    if (
        existing is not None
        and existing.platform == VS_DOMAIN
        and not _named_for_another_device(hass, entry, existing)
    ):
        _LOGGER.debug("Kiosk Satellite %s keeps its binding to %s", name, current)
        return current

    try:
        vs_entry = await _async_satellite_entry(hass, name)
    except (HomeAssistantError, vol.Invalid, KeyError) as err:
        _LOGGER.warning("Could not create the Voice Satellite entry for %s: %s", name, err)
        return None
    satellite = _satellite_entity(hass, vs_entry) if vs_entry else None
    if satellite is None:
        _LOGGER.warning("No Voice Satellite entity found for %s; KS left unbound", name)
        return None
    try:
        await ks_api_client.patch_settings(session, host, token, {SATELLITE_SETTING: satellite}, pin=pin)
    except (KsApiError, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Could not bind %s to %s: %s", name, satellite, err)
        return None
    _LOGGER.info("Kiosk Satellite %s bound to %s", name, satellite)
    return satellite


async def async_install_voice_satellite(hass: HomeAssistant) -> str | None:
    """KSM-BEHAVE-101: download Voice Satellite through HACS, making the same
    calls as HACS's `hacs/repository/download` handler. Returns an abort
    reason, or None on success."""
    hacs = hass.data.get(HACS_DOMAIN)
    if hacs is None:
        return "hacs_missing"
    repository = hacs.repositories.get_by_full_name(VS_REPOSITORY)
    if repository is None:
        return "repository_unknown"
    try:
        was_installed = repository.data.installed
        await repository.async_download_repository()
        if not was_installed:
            await hacs.async_recreate_entities()
        await hacs.data.async_write()
    except Exception as err:  # noqa: BLE001 -- HacsException is private to HACS
        _LOGGER.warning("HACS could not download %s: %s", VS_REPOSITORY, err)
        return "download_failed"
    return None


async def async_bind_all(hass: HomeAssistant) -> None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTRY_TYPE) is None and entry.state is ConfigEntryState.LOADED:
            await async_bind_voice_satellite(hass, entry)
