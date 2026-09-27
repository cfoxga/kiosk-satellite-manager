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
from urllib.parse import urlsplit

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import persistent_notification
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.helpers.network import get_url

from . import device_owner, ks_api_client, ks_tls
from .adb_client import AdbAuthPending, AdbClient, AdbConnectFailed, ensure_adb_key
from .const import (
    CONF_AREA_ID,
    CONF_AUTO_UPDATE,
    CONF_TLS_SPKI,
    CONF_DEVICE_PROFILE,
    CONF_ENTRY_TYPE,
    CONF_EXISTING_INSTALL_ACTION,
    CONF_HA_URL,
    CONF_HA_TOKEN,
    CONF_HOME_LAUNCHER,
    CONF_HOST,
    CONF_KEY_PATH,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_ONBOARDING_MODE,
    CONF_TOKEN_MODE,
    CONNECT_RETRY_ATTEMPTS,
    CONNECT_RETRY_DELAY_S,
    DEFAULT_ADB_PORT,
    DOMAIN,
    ENTRY_TYPE_MANAGER,
    MANAGER_UNIQUE_ID,
    ONBOARDING_AUTOMATIC,
    ONBOARDING_REVIEW,
    EXISTING_INSTALL_REINSTALL,
    EXISTING_INSTALL_REUSE,
    TOKEN_MODE_AUTO,
)

from .device_catalog import require_recipe, resolve_catalog_entry
from .credentials import TokenCredential, async_revoke_owned_credential
from .device_models import DeviceFacts
from .install import install_and_launch
from .ks_api_client import KsApiError, login

_LOGGER = logging.getLogger(__name__)

# Read-only, and used only to *read a label* off a device the catalog could not
# identify. It grants nothing and provisions nothing -- an unidentified device
# still gets no recipe (KSM-BEHAVE-048).
DEFAULT_DEVICE_NAME_COMMAND = "settings get global device_name"


async def _async_probe_ks_health(
    session: aiohttp.ClientSession, host: str
) -> tuple[str | None, dict] | None:
    """KSM-BEHAVE-096: (served HTTPS pin or None, health) when Kiosk Satellite
    answers `/api/health` on `host`, else None. Unauthenticated; no ADB."""
    probed = await ks_api_client.probe_https(session, host)
    if probed is not None:
        pin, health = probed
    else:
        pin = None
        try:
            health = await ks_api_client.get_health(session, host, pin=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
    if not isinstance(health, dict) or not health.get("appVersion"):
        return None
    return pin, health


async def _collect_identity_facts(client) -> DeviceFacts:
    """Read the allowlisted getprop identity facts the catalog matches on.

    Each probe is independent: one unreadable property leaves that fact empty
    rather than aborting discovery, and an empty fact never satisfies a match
    constraint.
    """

    async def _prop(name: str) -> str:
        try:
            return (await client.getprop(name)).strip()
        except Exception:  # noqa: BLE001 -- an unreadable prop is missing evidence
            return ""

    sdk_raw = await _prop("ro.build.version.sdk")
    return DeviceFacts(
        manufacturer=await _prop("ro.product.manufacturer"),
        brand=await _prop("ro.product.brand"),
        model=await _prop("ro.product.model"),
        product=await _prop("ro.product.name"),
        device=await _prop("ro.product.device"),
        board=await _prop("ro.product.board"),
        hardware=await _prop("ro.hardware"),
        characteristics=await _prop("ro.build.characteristics"),
        abi=await _prop("ro.product.cpu.abi"),
        sdk=int(sdk_raw) if sdk_raw.isdigit() else 0,
        fingerprint=await _prop("ro.build.fingerprint"),
    )


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY_TYPE, default="device"): selector.SelectSelector(
            selector.SelectSelectorConfig(options=[
                selector.SelectOptionDict(value="device", label="Add a device"),
                selector.SelectOptionDict(value=ENTRY_TYPE_MANAGER, label="Configure KSM"),
            ])
        ),
        vol.Optional(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_ADB_PORT): int,
    }
)


def _valid_ha_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and not parsed.username


def _manager_options(hass) -> dict:
    """Snapshot manager defaults without coupling an existing device to it."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
            return dict(entry.options)
    return {}


def _token_options(hass) -> list[selector.SelectOptionDict]:
    options = [selector.SelectOptionDict(value=TOKEN_MODE_AUTO, label="<Auto-create new token>")]
    for token in hass.auth._store.async_get_refresh_tokens():  # noqa: SLF001
        if token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
            options.append(selector.SelectOptionDict(
                value=token.id, label=token.client_name or "Unnamed long-lived token"
            ))
    return options


class KioskSatelliteManagerOptionsFlow(config_entries.OptionsFlow):
    """Manager defaults or one device's stored Kiosk Satellite password."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if self._entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_MANAGER:
            return await self.async_step_device_menu()
        saved = self._entry.options
        errors = {}
        if user_input is not None:
            token_id = user_input.get(CONF_TOKEN_MODE, TOKEN_MODE_AUTO)
            if token_id != TOKEN_MODE_AUTO:
                token = self.hass.auth.async_get_refresh_token(token_id)
                if token is None or token.token_type != TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
                    errors[CONF_TOKEN_MODE] = "token_not_found"
            if not _valid_ha_url(user_input[CONF_HA_URL]):
                errors[CONF_HA_URL] = "invalid_ha_url"
            if not errors:
                options = {**saved, **user_input}
                if not user_input.get(CONF_PASSWORD):
                    options[CONF_PASSWORD] = saved.get(CONF_PASSWORD, "")
                return self.async_create_entry(title="", data=options)
        try:
            default_url = get_url(self.hass, prefer_external=False)
        except Exception:
            default_url = ""
        fields = {
            vol.Required(CONF_EXISTING_INSTALL_ACTION, default=saved.get(
                CONF_EXISTING_INSTALL_ACTION, EXISTING_INSTALL_REUSE
            )): vol.In([EXISTING_INSTALL_REUSE, EXISTING_INSTALL_REINSTALL]),
            vol.Required(CONF_HOME_LAUNCHER, default=saved.get(CONF_HOME_LAUNCHER, True)): bool,
            vol.Required(CONF_AUTO_UPDATE, default=saved.get(CONF_AUTO_UPDATE, False)): bool,
            vol.Optional(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_HA_URL, default=saved.get(CONF_HA_URL, default_url)): str,
            vol.Required(CONF_TOKEN_MODE, default=saved.get(
                CONF_TOKEN_MODE, TOKEN_MODE_AUTO
            )): selector.SelectSelector(selector.SelectSelectorConfig(
                options=_token_options(self.hass), custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )),
            vol.Required(CONF_ONBOARDING_MODE, default=saved.get(
                CONF_ONBOARDING_MODE, ONBOARDING_REVIEW
            )): vol.In([ONBOARDING_REVIEW, ONBOARDING_AUTOMATIC]),
        }
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields), errors=errors)

    async def async_step_device_menu(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-088: a device entry's Configure is a menu."""
        return self.async_show_menu(
            step_id="device_menu", menu_options=["device_password", "device_owner"]
        )

    async def async_step_device_password(self, user_input: dict | None = None) -> FlowResult:
        """Verify a replacement secret before saving it on this device entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = user_input.get(CONF_PASSWORD, "")
            if not candidate:
                errors[CONF_PASSWORD] = "password_required"
            else:
                try:
                    await login(
                        async_get_clientsession(self.hass),
                        self._entry.data[CONF_HOST],
                        candidate,
                        pin=self._entry.data.get(CONF_TLS_SPKI),
                    )
                except Exception:  # noqa: BLE001 -- never expose a device response or secret
                    errors[CONF_PASSWORD] = "password_verification_failed"
                else:
                    self.hass.config_entries.async_update_entry(
                        self._entry,
                        data={**self._entry.data, CONF_PASSWORD: candidate},
                    )
                    return self.async_create_entry(title="", data=dict(self._entry.options))

        return self.async_show_form(
            step_id="device_password",
            data_schema=vol.Schema({
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }),
            errors=errors,
        )

    def _owner_client(self) -> AdbClient:
        data = self._entry.data
        return AdbClient(data[CONF_HOST], data[CONF_PORT], data[CONF_KEY_PATH])

    async def async_step_device_owner(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-088..090: explain Device Owner, confirm, then enroll.

        Preflight is read-only; nothing on the device changes until the user
        ticks the confirmation and submits."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if coordinator is not None and getattr(coordinator, "ksm_installing", False):
            return self.async_abort(reason="install_in_progress")
        model_key = self._entry.data.get(CONF_DEVICE_PROFILE)
        address = f"{self._entry.data[CONF_HOST]}:{self._entry.data[CONF_PORT]}"
        errors: dict[str, str] = {}

        if user_input is not None and user_input.get("confirm"):
            client = self._owner_client()
            # Hold the entry's install lock so no install/update runs ADB
            # against this device mid-enrollment (button.async_install_entry).
            if coordinator is not None:
                coordinator.ksm_installing = True
                coordinator.async_update_listeners()
            try:
                await client.connect()
                await device_owner.enable_device_owner(client, model_key)
            except (AdbAuthPending, AdbConnectFailed):
                return self.async_abort(
                    reason="adb_unavailable", description_placeholders={"address": address}
                )
            except device_owner.DeviceOwnerError as err:
                _LOGGER.warning("Device Owner enrollment failed on %s: %s", address, err)
                return self.async_abort(
                    reason="device_owner_failed",
                    description_placeholders={"reason": _owner_failure_text(err)},
                )
            except Exception as err:  # noqa: BLE001 -- ADB transport drop mid-run
                _LOGGER.warning("Device Owner enrollment error on %s: %s", address, err)
                return self.async_abort(
                    reason="device_owner_failed",
                    description_placeholders={"reason": "The ADB connection failed mid-run."},
                )
            finally:
                await client.close()
                if coordinator is not None:
                    coordinator.ksm_installing = False
                    coordinator.async_update_listeners()
            return self.async_abort(reason="device_owner_enabled")
        if user_input is not None:
            errors["confirm"] = "confirm_required"

        client = self._owner_client()
        try:
            await client.connect()
            pre = await device_owner.run_preflight(client, model_key)
        except (AdbAuthPending, AdbConnectFailed, OSError):
            return self.async_abort(
                reason="adb_unavailable", description_placeholders={"address": address}
            )
        finally:
            await client.close()

        if device_owner.BLOCKER_ALREADY_OWNER in pre.blockers:
            return self.async_abort(reason="device_owner_already")
        if pre.blockers:
            return self.async_abort(
                reason="device_owner_blocked",
                description_placeholders={"reason": _owner_blocker_text(pre)},
            )
        return self.async_show_form(
            step_id="device_owner",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
            description_placeholders={"accounts": _owner_accounts_text(pre)},
        )


_OWNER_BLOCKERS = {
    device_owner.BLOCKER_OTHER_OWNER: "another app ({owner}) is already Device Owner",
    device_owner.BLOCKER_MULTIPLE_USERS: "the device has more than one Android user",
    device_owner.BLOCKER_KS_MISSING: "Kiosk Satellite is not installed",
    device_owner.BLOCKER_UNOBSERVED: "the device's account or policy state could not be read",
    device_owner.BLOCKER_ACCOUNTS: (
        "it has an account KSM cannot safely clear on this model ({accounts}); "
        "remove it in Android Settings or factory reset"
    ),
}

_OWNER_FAILURES = {
    "clear_failed": "The account package could not be removed. Nothing else changed.",
    "accounts_remain": "Accounts were still present after clearing. Device Owner was not set.",
    "set_owner_failed": "Android refused to set Device Owner.",
    "readback_failed": "Android reported success, but Kiosk Satellite is not listed as Device Owner.",
    "preflight_blocked": "The device changed since the check; open Enable Device Owner again.",
}


def _owner_accounts_text(pre: "device_owner.Preflight") -> str:
    if not pre.account_counts:
        return "None. No app will be removed."
    lines = [
        f"- {count} × {acct_type} (from {pre.account_owners.get(acct_type) or 'unknown app'})"
        for acct_type, count in sorted(pre.account_counts.items())
    ]
    lines.append(
        "These are removed together with their app for a moment, then the app is "
        "reinstalled: " + ", ".join(pre.clear_packages)
    )
    return "\n".join(lines)


def _owner_blocker_text(pre: "device_owner.Preflight") -> str:
    blocking = sorted(
        t for t, pkg in pre.account_owners.items() if pkg not in pre.clear_packages
    )
    parts = [
        _OWNER_BLOCKERS[b].format(owner=pre.owner_package or "unknown", accounts=", ".join(blocking))
        for b in pre.blockers
    ]
    return "; ".join(parts)


def _owner_failure_text(err: "device_owner.DeviceOwnerError") -> str:
    if err.code == "restore_failed":
        return f"Device Owner step finished, but an app was not restored: {err.detail}"
    return _OWNER_FAILURES.get(err.code, err.code)


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
        self._home_launcher_supported: bool = False
        self._token_mode: str = TOKEN_MODE_AUTO
        self._credential: TokenCredential | None = None
        self._reuse_entry_id: str | None = None
        self._ks_installed: bool = False
        self._existing_install_action: str | None = None
        self._install_task: asyncio.Task | None = None
        self._tls_pin: str | None = None
        self._ks_probe_pin: str | None = None
        self._global: dict | None = None
        self._ha_url: str | None = None
        self._auto_update: bool = False

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return KioskSatelliteManagerOptionsFlow(config_entry)

    async def async_step_import(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-078: auto-create the manager entry from `async_setup`.

        Shares the same unique ID guard as the explicit "Configure KSM"
        choice in `async_step_user`, so this can never create a second
        manager entry alongside one a user already created by hand.
        """
        await self.async_set_unique_id(MANAGER_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Kiosk Satellite Manager", data={CONF_ENTRY_TYPE: ENTRY_TYPE_MANAGER}
        )

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Collect host/port, connect over ADB, and detect the device."""
        errors: dict[str, str] = {}
        if self._global is None:
            self._global = _manager_options(self.hass)
        if user_input is not None:
            if user_input.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
                await self.async_set_unique_id(MANAGER_UNIQUE_ID)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Kiosk Satellite Manager", data={CONF_ENTRY_TYPE: ENTRY_TYPE_MANAGER}
                )
            if not user_input.get(CONF_HOST):
                errors[CONF_HOST] = "host_required"
                return self.async_show_form(
                    step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
                )
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_ADB_PORT)
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            ks = await _async_probe_ks_health(async_get_clientsession(self.hass), host)
            if ks is not None:
                return await self._async_start_ks_device(host, port, *ks)

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
                facts = await _collect_identity_facts(client)
                sdk = facts.sdk
                try:
                    android_release = await client.getprop("ro.build.version.release")
                except Exception:
                    android_release = ""
                # Identity first, recipe second (KSM-BEHAVE-048). An
                # unrecognized device still gets a name and an entry -- it just
                # gets no recipe, and the Install button will refuse until the
                # catalog has an exact model row for it.
                entry = resolve_catalog_entry(facts)
                name_command = (
                    entry.recipe.device_name_command
                    if entry.recipe
                    else DEFAULT_DEVICE_NAME_COMMAND
                )
                raw_name = await client.shell(name_command)
                discovered_name = (
                    entry.recipe.normalize_device_name(raw_name)
                    if entry.recipe
                    else raw_name.strip()
                )
                ks_installed = await client.is_ks_installed()
            finally:
                await client.close()

            self._host = host
            self._port = port
            self._key_path = key_path
            self._profile_key = entry.model_key
            self._profile_name = entry.model_name or entry.classification_name
            self._home_launcher_supported = bool(
                entry.recipe and entry.recipe.home_launcher_supported
            )
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
                if self._global.get(CONF_ONBOARDING_MODE) == ONBOARDING_AUTOMATIC:
                    return await self.async_step_confirm()
                return await self.async_step_existing_install()
            if self._global.get(CONF_ONBOARDING_MODE) == ONBOARDING_AUTOMATIC:
                return await self.async_step_confirm()
            return await self.async_step_device_info()


        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        """Confirm the snapshot before automatic onboarding mutates a device."""
        action = (
            self._global.get(CONF_EXISTING_INSTALL_ACTION, EXISTING_INSTALL_REUSE)
            if self._ks_installed else "install"
        )
        errors = {}
        if user_input is not None:
            if not self._global.get(CONF_PASSWORD):
                errors["base"] = "password_required"
            token_mode = self._global.get(CONF_TOKEN_MODE, TOKEN_MODE_AUTO)
            credential = None
            if token_mode != TOKEN_MODE_AUTO:
                token = self.hass.auth.async_get_refresh_token(token_mode)
                if token is None or token.token_type != TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
                    errors["base"] = "token_not_found"
                else:
                    credential = TokenCredential(
                        self.hass.auth.async_create_access_token(token), token.id, owned=False
                    )
            try:
                require_recipe(self._profile_key)
            except Exception:
                errors["base"] = "unsupported_device"
            ha_url = self._global.get(CONF_HA_URL)
            if ha_url and not _valid_ha_url(ha_url):
                errors["base"] = "invalid_ha_url"
            if not errors:
                self._name = self._discovered_name
                self._password = self._global[CONF_PASSWORD]
                self._home_launcher = bool(
                    self._home_launcher_supported and self._global.get(CONF_HOME_LAUNCHER, True)
                )
                self._existing_install_action = action if self._ks_installed else None
                self._token_mode = token_mode
                self._credential = credential
                self._ha_url = ha_url
                self._auto_update = self._global.get(CONF_AUTO_UPDATE, False)
                return await self.async_step_install()
        return self.async_show_form(
            step_id="confirm", data_schema=vol.Schema({}), errors=errors,
            description_placeholders={
                "device": self._discovered_name, "action": action,
                "launcher": "yes" if self._home_launcher_supported and
                    self._global.get(CONF_HOME_LAUNCHER, True) else "no",
                "ha_url": self._global.get(CONF_HA_URL, "Home Assistant default"),
            },
        )

    async def async_step_device_info(self, user_input: dict | None = None) -> FlowResult:
        """Collect KSM settings after detection, excluding HA-owned naming."""
        errors: dict[str, str] = {}
        token_mode_options = _token_options(self.hass)

        if user_input is not None:
            password = user_input.get(CONF_PASSWORD) or self._global.get(CONF_PASSWORD, "")
            home_launcher = (
                user_input.get(CONF_HOME_LAUNCHER, self._global.get(CONF_HOME_LAUNCHER, True))
                if self._home_launcher_supported
                else False
            )
            token_mode = user_input.get(
                CONF_TOKEN_MODE, self._global.get(CONF_TOKEN_MODE, TOKEN_MODE_AUTO)
            )
            ha_url = user_input.get(CONF_HA_URL) or self._global.get(CONF_HA_URL)
            if ha_url and not _valid_ha_url(ha_url):
                errors[CONF_HA_URL] = "invalid_ha_url"
            credential = None

            if token_mode != TOKEN_MODE_AUTO:
                token = self.hass.auth.async_get_refresh_token(token_mode)
                if token is None or token.token_type != TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
                    errors[CONF_TOKEN_MODE] = "token_not_found"
                else:
                    credential = TokenCredential(
                        self.hass.auth.async_create_access_token(token), token.id, owned=False
                    )

            if not errors:
                self._name = self._discovered_name
                self._password = password
                self._home_launcher = home_launcher
                self._token_mode = token_mode
                self._credential = credential
                self._ha_url = ha_url
                self._auto_update = user_input.get(
                    CONF_AUTO_UPDATE, self._global.get(CONF_AUTO_UPDATE, False)
                )
                return await self.async_step_install()

        fields: dict[vol.Marker, Any] = {
            vol.Required(CONF_PASSWORD, default=self._global.get(CONF_PASSWORD, vol.UNDEFINED)): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_TOKEN_MODE, default=self._global.get(CONF_TOKEN_MODE, TOKEN_MODE_AUTO)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=token_mode_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    # A token can be revoked after this form was rendered.
                    # Let submission reach the authoritative lookup below so
                    # the flow returns token_not_found instead of a schema
                    # exception from a stale selector value.
                    custom_value=True,
                )
            ),
        }
        if self._home_launcher_supported:
            fields[vol.Required(CONF_HOME_LAUNCHER, default=self._global.get(CONF_HOME_LAUNCHER, True))] = (
                selector.BooleanSelector()
            )
        if self._global:
            fields[vol.Optional(CONF_HA_URL, default=self._global.get(CONF_HA_URL, ""))] = str
            fields[vol.Required(CONF_AUTO_UPDATE, default=self._global.get(CONF_AUTO_UPDATE, False))] = bool

        return self.async_show_form(
            step_id="device_info",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "device_model": self._profile_name or "Android Device",
                "android_version": self._android_version or "Unknown",
            },
        )

    async def _async_start_ks_device(
        self, host: str, port: int, pin: str | None, health: dict
    ) -> FlowResult:
        """KSM-BEHAVE-096: Kiosk Satellite already answers on this host, so
        identify it from its own health report and never open ADB."""
        # A local key only (no device contact), so the ADB-only buttons work
        # once the operator enables ADB.
        self._key_path = await self.hass.async_add_executor_job(
            ensure_adb_key, self.hass.config.path(DOMAIN)
        )
        entry = resolve_catalog_entry(DeviceFacts.from_health(health))
        self._host = host
        self._port = port
        self._ks_probe_pin = pin
        self._profile_key = entry.model_key
        self._profile_name = entry.model_name or entry.classification_name
        release = str(health.get("androidVersion") or "").strip()
        sdk = health.get("sdkInt")
        self._android_version = (
            f"{release} (SDK {sdk})" if release and sdk else release or "Unknown"
        )
        name = str(health.get("name") or "").strip()
        self._discovered_name = name or host
        self._ks_installed = True
        self._existing_install_action = EXISTING_INSTALL_REUSE
        return await self.async_step_ks_device_info()

    async def async_step_ks_device_info(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-096: verify the existing Kiosk Satellite password over
        the device's own API (pinned HTTPS when it can), then create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            session = async_get_clientsession(self.hass)
            pin: str | None = None
            try:
                if self._ks_probe_pin is None:
                    # HTTP device: check the password before the TLS switch,
                    # which would otherwise report a bad one as a TLS failure.
                    await login(session, self._host, password, pin=None)
            except KsApiError:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect_ks"
            if not errors:
                try:
                    pin = await ks_tls.async_establish_tls(session, self._host, password)
                except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError) as err:
                    _LOGGER.warning("Could not switch %s to HTTPS: %s", self._host, err)
                    errors["base"] = "cannot_connect_ks"
            if not errors and pin is not None:
                try:
                    await login(session, self._host, password, pin=pin)
                except KsApiError:
                    errors["base"] = "invalid_auth"
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    errors["base"] = "cannot_connect_ks"
            if not errors:
                global_opts = self._global or {}
                data = {
                    CONF_HOST: self._host,
                    CONF_PORT: self._port,
                    CONF_KEY_PATH: self._key_path,
                    CONF_DEVICE_PROFILE: self._profile_key,
                    CONF_NAME: self._discovered_name,
                    CONF_AREA_ID: None,
                    CONF_PASSWORD: password,
                    CONF_HA_URL: global_opts.get(CONF_HA_URL),
                }
                if pin:
                    data[CONF_TLS_SPKI] = pin
                return self.async_create_entry(
                    title=self._discovered_name,
                    data=data,
                    options={CONF_AUTO_UPDATE: global_opts.get(CONF_AUTO_UPDATE, False)},
                )

        return self.async_show_form(
            step_id="ks_device_info",
            data_schema=vol.Schema({
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }),
            errors=errors,
            description_placeholders={
                "device_model": self._profile_name or "Android Device",
                "android_version": self._android_version or "Unknown",
                "name": self._discovered_name,
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
                        CONF_EXISTING_INSTALL_ACTION, default=self._global.get(
                            CONF_EXISTING_INSTALL_ACTION, EXISTING_INSTALL_REUSE
                        )
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
        errors = {}
        if user_input is not None:
            token_mode = user_input.get(
                CONF_TOKEN_MODE, self._global.get(CONF_TOKEN_MODE, TOKEN_MODE_AUTO)
            )
            credential = None
            if token_mode != TOKEN_MODE_AUTO:
                token = self.hass.auth.async_get_refresh_token(token_mode)
                if token is None or token.token_type != TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
                    errors[CONF_TOKEN_MODE] = "token_not_found"
                else:
                    credential = TokenCredential(
                        self.hass.auth.async_create_access_token(token), token.id, owned=False
                    )
            if user_input.get(CONF_HA_URL) and not _valid_ha_url(user_input[CONF_HA_URL]):
                errors[CONF_HA_URL] = "invalid_ha_url"
        if user_input is not None and not errors:
            self._name = self._discovered_name
            self._area_id = None
            self._password = user_input.get(CONF_PASSWORD) or self._global.get(CONF_PASSWORD, "")
            self._ha_url = user_input.get(CONF_HA_URL) or self._global.get(CONF_HA_URL)
            self._auto_update = user_input.get(
                CONF_AUTO_UPDATE, self._global.get(CONF_AUTO_UPDATE, False)
            )
            self._token_mode = token_mode
            self._credential = credential
            self._home_launcher = bool(
                self._home_launcher_supported and self._global.get(CONF_HOME_LAUNCHER, True)
            )
            return await self.async_step_install()

        fields = {
            vol.Required(CONF_PASSWORD, default=self._global.get(CONF_PASSWORD, vol.UNDEFINED)):
                selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
        }
        if self._global:
            fields[vol.Optional(CONF_HA_URL, default=self._global.get(CONF_HA_URL, ""))] = str
            fields[vol.Required(CONF_AUTO_UPDATE, default=self._global.get(CONF_AUTO_UPDATE, False))] = bool
            fields[vol.Required(CONF_TOKEN_MODE, default=self._global.get(CONF_TOKEN_MODE, TOKEN_MODE_AUTO))] = (
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=_token_options(self.hass), custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                ))
            )
        return self.async_show_form(
            step_id="existing_device_info",
            data_schema=vol.Schema(fields),
            errors=errors,
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
                # KSM-BEHAVE-048: resolve the recipe before *any* device
                # mutation, not just before install_and_launch's own. The
                # reinstall path uninstalls first, so a model that fails closed
                # one line later would leave the device with its working app
                # removed and no approved recipe able to put it back.
                # Connecting is a pairing handshake, not a mutation, so it may
                # precede the gate; `uninstall_ks` may not.
                require_recipe(self._profile_key)
                if self._existing_install_action == EXISTING_INSTALL_REINSTALL:
                    await client.uninstall_ks()
                used_token = await install_and_launch(
                    self.hass,
                    client,
                    session,
                    host=self._host,
                    device_name=self._name,
                    password=self._password,
                    ha_token=self._credential.access_token if self._credential else None,
                    token_credential=self._credential,
                    home_launcher=self._home_launcher,
                    device_model=self._profile_key,
                    ha_url=self._ha_url,
                    on_tls_pinned=self._set_tls_pin,
                )
                if used_token:
                    self._credential = used_token
            finally:
                await client.close()

        except Exception:  # noqa: BLE001 -- see docstring
            _LOGGER.exception(
                "automatic install failed for %s; use the Install button to retry",
                self._host,
            )
            persistent_notification.async_create(
                self.hass,
                message=(
                    f"Kiosk Satellite could not be installed automatically on "
                    f"{self._host}. The device was still added to Kiosk Satellite "
                    "Manager; use its Install/Reinstall button to retry."
                ),
                title="Kiosk Satellite automatic installation failed",
                notification_id=f"{DOMAIN}_install_failed_{self._host}",
            )

    def _set_tls_pin(self, pin: str) -> None:
        """KSM-BEHAVE-094: the device key onboarding pinned, stored in the
        created entry."""
        self._tls_pin = pin

    async def async_step_install_done(self, user_input: dict | None = None) -> FlowResult:
        data = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_KEY_PATH: self._key_path,
            CONF_DEVICE_PROFILE: self._profile_key,
            CONF_NAME: self._name,
            CONF_AREA_ID: self._area_id,
            CONF_PASSWORD: self._password,
            CONF_HA_URL: self._ha_url,
        }
        if self._tls_pin:
            data[CONF_TLS_SPKI] = self._tls_pin
        if self._existing_install_action != EXISTING_INSTALL_REUSE or self._global:
            data.update(
                {
                    CONF_HOME_LAUNCHER: self._home_launcher,
                    CONF_TOKEN_MODE: self._token_mode,
                    **(self._credential.as_entry_data() if self._credential else {}),
                }
            )
        return self.async_create_entry(
            title=self._name,
            data=data,
            options={CONF_AUTO_UPDATE: self._auto_update},
        )

    def async_abort(self, *, reason: str, description_placeholders=None, next_flow=None) -> FlowResult:
        """Revoke an auto-created token when the operator abandons this flow."""
        self.hass.async_create_task(async_revoke_owned_credential(self.hass, self._credential))
        return super().async_abort(
            reason=reason,
            description_placeholders=description_placeholders,
            next_flow=next_flow,
        )
