"""Repair flows.

TLS key changed (KSM-BEHAVE-095, #57): the operator's confirmation is the
trust event -- the flow re-probes the device over HTTPS and pins whatever key
it serves now. Until then every management call stays blocked by the old pin.

New fleet follower (KSM-BEHAVE-146): confirming starts that follower's
Discovered card, where the operator enters its password.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import follower_offers, ks_api_client, fleet
from .const import CONF_HOST, CONF_TLS_SPKI


class TlsCertificateChangedFlow(RepairsFlow):
    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        entry = fleet.resolve_device(self.hass, self._entry_id)
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
        fleet.update_device(self.hass, entry, data={**entry.data, CONF_TLS_SPKI: probed[0]})
        coordinator = self.hass.data.get(entry.domain, {}).get(entry.entry_id)
        if coordinator is not None:
            await coordinator.async_request_refresh()
        return self.async_create_entry(data={})


class NewFollowerFlow(RepairsFlow):
    """KSM-BEHAVE-146: confirming asks through the follower's Discovered card."""

    def __init__(self, offer: dict) -> None:
        self._offer = offer

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="confirm", data_schema=vol.Schema({}),
                description_placeholders={"name": self._offer["name"], "host": self._offer["host"]},
            )
        await follower_offers.async_offer(self.hass, self._offer)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    if issue_id.startswith(follower_offers.ISSUE_PREFIX):
        return NewFollowerFlow(dict(data or {}))
    return TlsCertificateChangedFlow((data or {})["entry_id"])
