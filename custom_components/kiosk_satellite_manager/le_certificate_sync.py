"""Keep opted-in KS devices on the certificate renewed by HA's add-on."""

from __future__ import annotations

from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import fleet, ks_api_client, ks_tls, le_certificate
from .const import (
    CONF_HOST, CONF_PASSWORD, CONF_TLS_SPKI, DOMAIN,
    CONF_LE_CERTIFICATE_HOSTNAME, CONF_LE_CERTIFICATE_FINGERPRINT,
)
from .device_repairs import tls_issue_id

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
    session = async_get_clientsession(hass)
    # KSM-BEHAVE-180: an earlier import can land after its check gave up,
    # leaving the old pin stranded. The handshake proves the device holds HA's
    # private key, so serving HA's exact certificate is adopted as is.
    served = await ks_api_client.probe_https_identity(session, data[CONF_HOST])
    if served == (material.spki_sha256, material.fingerprint):
        fleet.update_device(hass, entry, data={
            **data, CONF_TLS_SPKI: material.spki_sha256,
            CONF_LE_CERTIFICATE_FINGERPRINT: material.fingerprint,
        })
        ir.async_delete_issue(hass, DOMAIN, tls_issue_id(entry.entry_id))
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coordinator is not None:
            await coordinator.async_request_refresh()
        return True
    new_pin = await ks_tls.async_import_certificate(
        session, data[CONF_HOST], data[CONF_PASSWORD], pin, material
    )
    fleet.update_device(hass, entry, data={
        **data, CONF_TLS_SPKI: new_pin,
        CONF_LE_CERTIFICATE_FINGERPRINT: material.fingerprint,
    })
    return True
