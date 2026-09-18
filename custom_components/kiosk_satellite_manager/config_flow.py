"""Config flow for Kiosk Satellite Manager.

Placeholder shell only — Phase 1 adds the real ADB-key generation, connect,
and on-device Allow-tap progress step described in docs/SPEC/provisioning.md.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required("host"): str})


class KioskSatelliteManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Kiosk Satellite Manager."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """First (and, for now, only) step: collect the device IP."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input["host"])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input["host"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )
