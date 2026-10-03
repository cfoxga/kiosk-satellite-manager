"""Repair flows.

TLS key changed (KSM-BEHAVE-095, #57): the operator's confirmation is the
trust event -- the flow re-probes the device over HTTPS and pins whatever key
it serves now. Until then every management call stays blocked by the old pin.

HTTPS turned off (KSM-BEHAVE-170, #137): confirming checks the device answers
over HTTP and drops the pin, returning it to plaintext management.

New fleet follower (KSM-BEHAVE-146): confirming starts that follower's
Discovered card, where the operator enters its password.

Device support (KSM-BEHAVE-165, #135): confirming reads the device's
capability report over ADB and shows a pre-filled support-request link.
"""
from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import area_registry as ar, device_registry as dr, issue_registry as ir, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import follower_offers, ks_api_client, fleet, support_request
from .adb_client import AdbAuthPending, AdbConnectFailed, AdbKeySecurityError
from .const import CONF_AREA_ID, CONF_HOST, CONF_TLS_SPKI, DOMAIN
from .device_repairs import SUPPORT_REQUESTED, area_required_issue_id, device_support_issue_id, tls_disabled_issue_id

_LOGGER = logging.getLogger(__name__)


class AreaRequiredFlow(RepairsFlow):
    """Assign the Area on both the HA device and KSM's stored device entry."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_area(user_input)

    async def async_step_area(self, user_input: dict | None = None) -> FlowResult:
        entry = fleet.resolve_device(self.hass, self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        device = dr.async_get(self.hass).async_get_device_by_identifier(
            (DOMAIN, self._entry_id), getattr(entry, "parent", entry).entry_id,
        )
        if device is None:
            return self.async_abort(reason="device_not_found")
        errors = {}
        if user_input is not None:
            area_id = user_input[CONF_AREA_ID]
            if ar.async_get(self.hass).async_get_area(area_id) is None:
                errors[CONF_AREA_ID] = "area_not_found"
            else:
                dr.async_get(self.hass).async_update_device(device.id, area_id=area_id)
                fleet.update_device(
                    self.hass, entry, data={**entry.data, CONF_AREA_ID: area_id}
                )
                ir.async_delete_issue(
                    self.hass, DOMAIN, area_required_issue_id(self._entry_id)
                )
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="area",
            data_schema=vol.Schema({vol.Required(CONF_AREA_ID): selector.AreaSelector()}),
            errors=errors,
            description_placeholders={"name": entry.title},
        )


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


class TlsDisabledFlow(RepairsFlow):
    """KSM-BEHAVE-170: the operator turned Use HTTPS off on the device."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        entry = fleet.resolve_device(self.hass, self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        host = entry.data[CONF_HOST]
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={"name": entry.title, "host": host},
            )
        try:
            await ks_api_client.get_health(async_get_clientsession(self.hass), host, pin=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return self.async_abort(reason="cannot_connect")
        fleet.update_device(
            self.hass, entry, data={k: v for k, v in entry.data.items() if k != CONF_TLS_SPKI}
        )
        ir.async_delete_issue(self.hass, DOMAIN, tls_disabled_issue_id(self._entry_id))
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


class DeviceSupportFlow(RepairsFlow):
    """KSM-BEHAVE-165: build the device's support request, then link to it."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._placeholders: dict[str, str] = {}

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        entry = fleet.resolve_device(self.hass, self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        if user_input is None:
            return self.async_show_form(
                step_id="confirm", data_schema=vol.Schema({}),
                description_placeholders={"name": entry.title},
            )
        try:
            request, url = await support_request.async_build_for_device(self.hass, entry)
        except (AdbConnectFailed, AdbAuthPending, AdbKeySecurityError, OSError, TimeoutError) as err:
            _LOGGER.warning("Support request for %s: ADB unavailable (%s)", entry.title, type(err).__name__)
            return self.async_abort(
                reason="cannot_connect_adb", description_placeholders={"name": entry.title}
            )
        if request["report"]["catalog"].get("executable_recipe"):
            # The library now knows this device; Install stores its model.
            ir.async_delete_issue(self.hass, DOMAIN, device_support_issue_id(self._entry_id))
            return self.async_abort(
                reason="now_supported", description_placeholders={"name": entry.title}
            )
        recipe = request["recipe"]
        self._placeholders = {
            "name": entry.title,
            "device": support_request.device_label(request),
            "kind": request["kind"],
            "model_key": (request["device_model"] or {}).get("model_key")
            or request["existing_model_key"] or "-",
            "candidate": recipe.get("candidate") or recipe.get("current") or "a new recipe",
            "url": url,
        }
        return await self.async_step_send()

    async def async_step_send(self, user_input: dict | None = None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="send", data_schema=vol.Schema({}),
                description_placeholders=self._placeholders,
            )
        entry = fleet.resolve_device(self.hass, self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        fleet.update_device(self.hass, entry, data={**entry.data, SUPPORT_REQUESTED: True})
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    if issue_id == area_required_issue_id((data or {}).get("entry_id", "")):
        return AreaRequiredFlow((data or {})["entry_id"])
    if issue_id.startswith(follower_offers.ISSUE_PREFIX):
        return NewFollowerFlow(dict(data or {}))
    if issue_id == device_support_issue_id((data or {}).get("entry_id", "")):
        return DeviceSupportFlow((data or {})["entry_id"])
    if issue_id == tls_disabled_issue_id((data or {}).get("entry_id", "")):
        return TlsDisabledFlow((data or {})["entry_id"])
    return TlsCertificateChangedFlow((data or {})["entry_id"])
