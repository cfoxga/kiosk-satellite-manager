"""Config flow for Kiosk Satellite Manager.

Phase 1: host/port -> generate-or-reuse an ADB key -> bounded-retry connect
(covers the on-device "Allow USB debugging?" tap -- Context's irreducible
manual step 2) -> detect device type via getprop -> create the entry.

KSM-BEHAVE-044: after detection, the KSM-specific step shows the detected
device type and Android version, then collects only settings KSM itself needs.
Home Assistant's post-entry "Name and assign" screen owns the entry name and
area, so this flow uses the detected Android name only for initial Kiosk
Satellite provisioning.

KSM-BEHAVE-012: rather than making the user press the Install button after
adding the integration, the flow now runs install.install_and_launch itself
as a progress step right after device_info, using HA's async_show_progress/
async_show_progress_done two-step protocol (unlike the plain retry loop
above -- this one genuinely can take long enough, and cross enough real
await points, that a bounded synchronous retry isn't the right shape).
Install failure is swallowed and logged rather than aborting the flow: the
device is already paired by this point (the hard, human-in-the-loop part),
so the entry is still created either way and the Install/Reinstall button
remains the recovery path, exactly as it already is for repeat
installs/upgrades on an existing entry.

A bounded retry loop rather than HA's async_show_progress two-step dance:
this repo's tests only get a real `hass` fixture in tests/integration/ (see
requirements_test.txt), and a plain retry has much less surface to get
subtly wrong there than the progress-step protocol, for the same user-facing
effect -- a wait while the on-device dialog gets tapped.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN

from .adb_client import AdbAuthPending, AdbClient, AdbConnectFailed, ensure_adb_key
from .const import (
    CONF_AREA_ID,
    CONF_DEVICE_PROFILE,
    CONF_EXISTING_INSTALL_ACTION,
    CONF_HA_TOKEN,
    CONF_HOME_LAUNCHER,
    CONF_HOST,
    CONF_KEY_PATH,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_TOKEN_MODE,
    CONNECT_RETRY_ATTEMPTS,
    CONNECT_RETRY_DELAY_S,
    DEFAULT_ADB_PORT,
    DOMAIN,
    EXISTING_INSTALL_REINSTALL,
    EXISTING_INSTALL_REUSE,
    TOKEN_MODE_AUTO,
)

from .device_profiles import match_profile
from .install import install_and_launch

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

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int | None = None
        self._key_path: str | None = None
        self._profile_key: str | None = None
        self._profile_name: str | None = None
        self._android_version: str | None = None
        self._discovered_name: str = ""
        self._name: str | None = None
        self._area_id: str | None = None
        self._password: str | None = None
        self._home_launcher: bool = True
        self._token_mode: str = TOKEN_MODE_AUTO
        self._ha_token: str | None = None
        self._reuse_entry_id: str | None = None
        self._ks_installed: bool = False
        self._existing_install_action: str | None = None
        self._install_task: asyncio.Task | None = None

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Collect host/port, connect over ADB, and detect the device."""
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
                try:
                    model = await client.getprop("ro.product.model")
                except Exception:
                    model = ""
                try:
                    sdk_str = await client.getprop("ro.build.version.sdk")
                    sdk = int(sdk_str) if sdk_str.isdigit() else 0
                except Exception:
                    sdk = 0
                try:
                    android_release = await client.getprop("ro.build.version.release")
                except Exception:
                    android_release = ""
                profile = match_profile(characteristics, manufacturer, model=model, sdk=sdk)
                discovered_name = profile.normalize_device_name(
                    await client.shell(profile.device_name_command)
                )
                ks_installed = await client.is_ks_installed()
            finally:
                await client.close()

            self._host = host
            self._port = port
            self._key_path = key_path
            self._profile_key = profile.key
            self._profile_name = profile.name
            self._android_version = (
                f"Android {android_release} (SDK {sdk})"
                if android_release
                else (f"SDK {sdk}" if sdk else "Unknown")
            )
            self._ks_installed = ks_installed
            self._discovered_name = (
                discovered_name if discovered_name and discovered_name != "null" else host
            )
            if self._ks_installed:
                return await self.async_step_existing_install()
            return await self.async_step_device_info()


        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_device_info(self, user_input: dict | None = None) -> FlowResult:
        """Collect KSM settings after detection, excluding HA-owned naming."""
        errors: dict[str, str] = {}
        token_mode_options = [
            selector.SelectOptionDict(value=TOKEN_MODE_AUTO, label="<Auto-create new token>"),
        ]
        # HA does not expose a public listing API for long-lived access tokens.
        # The auth store is the same source used by HA's profile token UI; only
        # identifiers and labels are placed in the form, never token secrets.
        long_lived_tokens = [
            token
            for token in self.hass.auth._store.async_get_refresh_tokens()  # noqa: SLF001
            if token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
        ]
        for token in long_lived_tokens:
            token_mode_options.append(
                selector.SelectOptionDict(
                    value=token.id,
                    label=token.client_name or "Unnamed long-lived token",
                )
            )

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            home_launcher = user_input.get(CONF_HOME_LAUNCHER, True)
            token_mode = user_input.get(CONF_TOKEN_MODE, TOKEN_MODE_AUTO)
            ha_token = None

            if token_mode != TOKEN_MODE_AUTO:
                token = self.hass.auth.async_get_refresh_token(token_mode)
                if token is None or token.token_type != TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
                    errors[CONF_TOKEN_MODE] = "token_not_found"
                else:
                    ha_token = self.hass.auth.async_create_access_token(token)

            if not errors:
                self._name = self._discovered_name
                self._password = password
                self._home_launcher = home_launcher
                self._token_mode = token_mode
                self._ha_token = ha_token
                return await self.async_step_install()

        fields: dict[vol.Marker, Any] = {
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_HOME_LAUNCHER, default=True): selector.BooleanSelector(),
            vol.Required(
                CONF_TOKEN_MODE, default=TOKEN_MODE_AUTO
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=token_mode_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }

        return self.async_show_form(
            step_id="device_info",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "device_model": self._profile_name or "Android Device",
                "android_version": self._android_version or "Unknown",
            },
        )

    async def async_step_existing_install(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """KSM-BEHAVE-021: Kiosk Satellite is already on this device -- let the
        user keep it as-is or replace it with a fresh install."""
        if user_input is not None:
            self._existing_install_action = user_input[CONF_EXISTING_INSTALL_ACTION]
            if self._existing_install_action == EXISTING_INSTALL_REUSE:
                return await self.async_step_existing_device_info()
            return await self.async_step_device_info()

        return self.async_show_form(
            step_id="existing_install",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EXISTING_INSTALL_ACTION, default=EXISTING_INSTALL_REUSE
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=EXISTING_INSTALL_REUSE,
                                    label="Keep the existing installation",
                                ),
                                selector.SelectOptionDict(
                                    value=EXISTING_INSTALL_REINSTALL,
                                    label="Uninstall and reinstall Kiosk Satellite",
                                ),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_existing_device_info(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """KSM-BEHAVE-029: collect only KSM's connection details when the
        user keeps the installed application's own settings intact."""
        if user_input is not None:
            self._name = self._discovered_name
            self._area_id = None
            self._password = user_input[CONF_PASSWORD]
            return await self.async_step_install()

        return self.async_show_form(
            step_id="existing_device_info",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            description_placeholders={"device_model": self._profile_name or "Android Device"},
        )

    async def async_step_install(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-012: run install_and_launch as a progress step so
        adding the integration installs the app automatically instead of
        requiring a separate button press afterward."""
        if not self._install_task:
            self._install_task = self.hass.async_create_task(self._async_do_install())

        if not self._install_task.done():
            return self.async_show_progress(
                step_id="install",
                progress_action="installing",
                progress_task=self._install_task,
            )

        return self.async_show_progress_done(next_step_id="install_done")

    async def _async_do_install(self) -> None:
        """Best-effort: any failure here is logged, not raised, so the
        entry still gets created and the Install/Reinstall button remains
        the recovery path -- exactly what already happens for repeat
        installs/upgrades on an existing entry."""
        if self._existing_install_action == EXISTING_INSTALL_REUSE:
            _LOGGER.debug("keeping existing Kiosk Satellite install on %s", self._host)
            return
        session = async_get_clientsession(self.hass)
        client = AdbClient(self._host, self._port, self._key_path)
        try:
            await client.connect()
            try:
                if self._existing_install_action == EXISTING_INSTALL_REINSTALL:
                    await client.uninstall_ks()
                used_token = await install_and_launch(
                    self.hass,
                    client,
                    session,
                    host=self._host,
                    device_name=self._name,
                    password=self._password,
                    ha_token=self._ha_token,
                    home_launcher=self._home_launcher,
                    device_profile=self._profile_key,
                )
                if used_token:
                    self._ha_token = used_token
            finally:
                await client.close()

        except Exception:  # noqa: BLE001 -- see docstring
            _LOGGER.exception(
                "automatic install failed for %s; use the Install button to retry",
                self._host,
            )

    async def async_step_install_done(self, user_input: dict | None = None) -> FlowResult:
        data = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_KEY_PATH: self._key_path,
            CONF_DEVICE_PROFILE: self._profile_key,
            CONF_NAME: self._name,
            CONF_AREA_ID: self._area_id,
            CONF_PASSWORD: self._password,
        }
        if self._existing_install_action != EXISTING_INSTALL_REUSE:
            data.update(
                {
                    CONF_HOME_LAUNCHER: self._home_launcher,
                    CONF_TOKEN_MODE: self._token_mode,
                    CONF_HA_TOKEN: self._ha_token,
                }
            )
        return self.async_create_entry(
            title=self._name,
            data=data,
        )
