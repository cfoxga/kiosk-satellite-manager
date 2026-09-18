"""Config flow for Kiosk Satellite Manager.

Phase 1: host/port -> generate-or-reuse an ADB key -> bounded-retry connect
(covers the on-device "Allow USB debugging?" tap -- Context's irreducible
manual step 2) -> detect device type via getprop -> create the entry.

A bounded retry loop rather than HA's async_show_progress two-step dance:
this repo's tests only get a real `hass` fixture in tests/integration/ (see
requirements_test.txt), and a plain retry has much less surface to get
subtly wrong there than the progress-step protocol, for the same user-facing
effect -- a wait while the on-device dialog gets tapped.
"""
from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .adb_client import AdbAuthPending, AdbClient, AdbConnectFailed, ensure_adb_key
from .const import (
    CONF_DEVICE_PROFILE,
    CONF_HOST,
    CONF_KEY_PATH,
    CONF_PORT,
    CONNECT_RETRY_ATTEMPTS,
    CONNECT_RETRY_DELAY_S,
    DEFAULT_ADB_PORT,
    DOMAIN,
)
from .device_profiles import match_profile

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_ADB_PORT): int,
    }
)


class KioskSatelliteManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Kiosk Satellite Manager."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Collect host/port, connect over ADB, detect the device, create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            key_path = await self.hass.async_add_executor_job(
                ensure_adb_key, self.hass.config.path(DOMAIN)
            )
            client = AdbClient(host, port, key_path)
            last_err: Exception | None = None
            connected = False
            for attempt in range(CONNECT_RETRY_ATTEMPTS):
                try:
                    await client.connect()
                    connected = True
                    break
                except AdbAuthPending as err:
                    last_err = err
                    if attempt < CONNECT_RETRY_ATTEMPTS - 1:
                        await asyncio.sleep(CONNECT_RETRY_DELAY_S)
                except AdbConnectFailed as err:
                    last_err = err
                    break

            if not connected:
                errors["base"] = (
                    "auth_pending" if isinstance(last_err, AdbAuthPending) else "cannot_connect"
                )
                _LOGGER.debug("connect to %s failed: %s", host, last_err)
                return self.async_show_form(
                    step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
                )

            try:
                characteristics = await client.getprop("ro.build.characteristics")
                manufacturer = await client.getprop("ro.product.manufacturer")
            finally:
                await client.close()

            profile = match_profile(characteristics, manufacturer)
            return self.async_create_entry(
                title=host,
                data={
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_KEY_PATH: key_path,
                    CONF_DEVICE_PROFILE: profile.key,
                },
            )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )
