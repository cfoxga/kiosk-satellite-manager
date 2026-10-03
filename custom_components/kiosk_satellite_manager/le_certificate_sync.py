"""Keep opted-in KS devices on the certificate renewed by HA's add-on."""

from __future__ import annotations

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import fleet, ks_tls, le_certificate
from .const import (
    CONF_HOST, CONF_PASSWORD, CONF_TLS_SPKI,
    CONF_LE_CERTIFICATE_HOSTNAME, CONF_LE_CERTIFICATE_FINGERPRINT,
)

async def async_sync_device(hass, entry):
    """Import a changed HA certificate; retain the old pin if any step fails."""
    data = entry.data
    hostname = data.get(CONF_LE_CERTIFICATE_HOSTNAME)
    pin = data.get(CONF_TLS_SPKI)
    if not hostname or not pin:
        return False
    material = await hass.async_add_executor_job(
        le_certificate.load_for_hostname, hostname
    )
    if material.fingerprint == data.get(CONF_LE_CERTIFICATE_FINGERPRINT):
        return False
    new_pin = await ks_tls.async_import_certificate(
        async_get_clientsession(hass), data[CONF_HOST], data[CONF_PASSWORD], pin, material
    )
    fleet.update_device(hass, entry, data={
        **data, CONF_TLS_SPKI: new_pin,
        CONF_LE_CERTIFICATE_FINGERPRINT: material.fingerprint,
    })
    return True
