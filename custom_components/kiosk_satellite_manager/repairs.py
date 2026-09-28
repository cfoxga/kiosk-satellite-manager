"""Repair flows.

TLS key changed (KSM-BEHAVE-095, #57): the operator's confirmation is the
trust event -- the flow re-probes the device over HTTPS and pins whatever key
it serves now. Until then every management call stays blocked by the old pin.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import ks_api_client
from .const import CONF_HOST, CONF_TLS_SPKI


class TlsCertificateChangedFlow(RepairsFlow):
    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={"name": entry.title, "host": entry.data[CONF_HOST]},
            )
        probed = await ks_api_client.probe_https(
            async_get_clientsession(self.hass), entry.data[CONF_HOST]
        )
        if probed is None:
            return self.async_abort(reason="cannot_connect")
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TLS_SPKI: probed[0]}
        )
        coordinator = self.hass.data.get(entry.domain, {}).get(entry.entry_id)
        if coordinator is not None:
            await coordinator.async_request_refresh()
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    return TlsCertificateChangedFlow((data or {})["entry_id"])
