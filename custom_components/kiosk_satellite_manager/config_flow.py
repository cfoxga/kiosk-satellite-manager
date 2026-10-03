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
import re
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import persistent_notification
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.helpers.network import get_url

from . import (
    apk_cache, device_owner, esphome_adopt, fleet, ks_api_client, ks_tls, meta_setup, support_log,
    support_request,
)
from .helpers import recent_releases
from .adb_client import AdbAuthPending, AdbClient, AdbConnectFailed, ensure_adb_key
from .const import (
    CONF_AREA_ID,
    CONF_AUTO_UPDATE,
    CONF_ENABLE_ESPHOME,
    CONF_ESPHOME_ENABLE_PENDING,
    CONF_ESPHOME_NEW_DEVICES,
    CONF_HIDE_FOLLOWER_UPDATES,
    CONF_TARGET_VERSION,
    TARGET_VERSION_LATEST,
    CONF_PRIVATE_DNS_PRIOR,
    CONF_TLS_SPKI,
    CONF_DEVICE_PROFILE,
    CONF_REPLACE_LAUNCHER,
    CONF_BACKUP_INTERVAL_HOURS,
    CONF_BACKUP_KEEP,
    CONF_ENABLE_DEVICE_OWNER,
    CONF_ENTRY_TYPE,
    CONF_EXISTING_INSTALL_ACTION,
    CONF_HA_URL,
    CONF_HA_TOKEN,
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
    HEALTH_PORT,
    DEFAULT_BACKUP_INTERVAL_HOURS,
    DEFAULT_BACKUP_KEEP,
    DOMAIN,
    ENTRY_TYPE_MANAGER,
    ENTRY_TYPE_UNMANAGED,
    ENTRY_TYPE_FLEET,
    MANAGER_UNIQUE_ID,
    UNMANAGED_UNIQUE_ID,
    ONBOARDING_AUTOMATIC,
    ONBOARDING_REVIEW,
    RENAME_API_KEY,
    EXISTING_INSTALL_REINSTALL,
    EXISTING_INSTALL_REUSE,
    EXISTING_INSTALL_UPDATE_SETTINGS,
    TOKEN_MODE_AUTO,
)

from .device_catalog import NoApprovedRecipe, require_recipe, resolve_catalog_entry
from .credentials import TokenCredential, async_revoke_owned_credential
from .device_models import DeviceFacts, collect_identity_facts
from .device_repairs import stash_dashboard_dns, tls_disabled_issue_id, tls_issue_id
from .install import (
    DashboardDnsCheck,
    async_connect_ha,
    install_and_launch,
    launcher_replacement_wanted,
)
from .ks_api_client import KsApiError, login
from .provisioning import fetch_health
from .rename import derive_rename_names

_LOGGER = logging.getLogger(__name__)
_HOST_FORBIDDEN = re.compile(r"[\s/@?#]")

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

FLEET_CHOICE = "fleet_entry_id"
UNMANAGED_CHOICE = "unmanaged"


def _fleet_options(hass) -> list[selector.SelectOptionDict]:
    """Existing KS fleets, not the synthetic Unmanaged entry."""
    return [selector.SelectOptionDict(value=UNMANAGED_CHOICE, label="Unmanaged"), *(
        selector.SelectOptionDict(value=entry.entry_id, label=entry.title)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_FLEET
    )]


def _onboarding_schema(hass) -> vol.Schema:
    fields = dict(STEP_USER_DATA_SCHEMA.schema)
    if len(options := _fleet_options(hass)) > 1:
        fields[vol.Optional(FLEET_CHOICE, default=UNMANAGED_CHOICE)] = (
            selector.SelectSelector(selector.SelectSelectorConfig(
                options=options, mode=selector.SelectSelectorMode.DROPDOWN,
            ))
        )
    return vol.Schema(fields)


def _invitation_leader(hass, fleet_entry_id: str) -> fleet.DeviceEntry | None:
    """Resolve a current, confirmed leader for the selected HA fleet."""
    parent = hass.config_entries.async_get_entry(fleet_entry_id)
    if parent is None or parent.domain != DOMAIN or parent.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_FLEET:
        return None
    leader_id = parent.data.get("leader_id")
    leaders = [device for device in fleet.device_entries(hass, parent)
               if device.fleet_status.get("self_id") == leader_id
               and device.fleet_status.get("leading") is True
               and fleet.status_available(hass, device.entry_id)]
    return leaders[0] if len(leaders) == 1 else None


async def _async_invite_to_fleet(
    hass, fleet_entry_id: str, target_host: str, target_password: str, target_pin: str | None,
) -> None:
    """Ask the selected leader to invite a verified KS device by address."""
    leader = _invitation_leader(hass, fleet_entry_id)
    if leader is None:
        raise KsApiError("selected Fleet has no available leader")
    data = leader.data
    session = async_get_clientsession(hass)
    target_token = await login(session, target_host, target_password, pin=target_pin)
    target_status = await ks_api_client.run_command(
        session, target_host, target_token, "fleetStatus", pin=target_pin,
    )
    target_data = target_status.get("data") if isinstance(target_status, dict) and target_status.get("ok") is True else None
    target_self = target_data.get("self") if isinstance(target_data, dict) else None
    target_id = target_self.get("id") if isinstance(target_self, dict) else None
    if not isinstance(target_id, str) or not target_id:
        raise KsApiError("new kiosk did not report a Fleet identity")
    pin = data.get(CONF_TLS_SPKI)
    token = await login(session, data[CONF_HOST], data[CONF_PASSWORD], pin=pin)
    found = await ks_api_client.run_command(
        session, data[CONF_HOST], token, "fleetLookup", pin=pin,
        params={"address": target_host, "port": HEALTH_PORT},
    )
    kiosk = found.get("data") if isinstance(found, dict) and found.get("ok") is True else None
    if not isinstance(kiosk, dict) or kiosk.get("id") != target_id or not kiosk.get("address"):
        raise KsApiError("leader lookup does not match the new kiosk")
    invited = await ks_api_client.run_command(
        session, data[CONF_HOST], token, "fleetInvite", pin=pin,
        params={"id": kiosk["id"], "address": kiosk["address"],
                "port": kiosk.get("port", HEALTH_PORT), "profile": "default"},
    )
    if not isinstance(invited, dict) or invited.get("ok") is not True:
        raise KsApiError("leader rejected the Fleet invitation")


def _valid_ha_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and not parsed.username


async def _native_home_enabled(hass, host: str, password: str, pin: str | None) -> bool:
    """Read KS's own Home choice; KSM never substitutes an Android setting."""
    if not password:
        return False
    session = async_get_clientsession(hass)
    token = await ks_api_client.login(session, host, password, pin=pin)
    settings = await ks_api_client.get_settings(session, host, token, pin=pin)
    return settings.get("home.enabled") is True


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
        if self._entry.data.get(CONF_ENTRY_TYPE) in (ENTRY_TYPE_UNMANAGED, ENTRY_TYPE_FLEET):
            return self.async_abort(reason="not_supported")
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
                options.pop("home_launcher", None)
                if not user_input.get(CONF_PASSWORD):
                    options[CONF_PASSWORD] = saved.get(CONF_PASSWORD, "")
                return self.async_create_entry(title="", data=options)
        try:
            default_url = get_url(self.hass, prefer_external=False)
        except Exception:
            default_url = ""
        # KSM-BEHAVE-114/116: Latest, then every downloaded version and enough
        # of the newest releases to offer at least five.
        cached = await self.hass.async_add_executor_job(
            apk_cache.cached_versions, apk_cache.cache_root(self.hass)
        )
        versions = apk_cache.install_version_choices(
            cached, [r.version for r in recent_releases(self.hass)]
        )
        saved_target = saved.get(CONF_TARGET_VERSION, TARGET_VERSION_LATEST)
        fields = {
            vol.Required(CONF_EXISTING_INSTALL_ACTION, default=saved.get(
                CONF_EXISTING_INSTALL_ACTION, EXISTING_INSTALL_REUSE
            )): vol.In([EXISTING_INSTALL_REUSE, EXISTING_INSTALL_REINSTALL,
                        EXISTING_INSTALL_UPDATE_SETTINGS]),
            vol.Required(CONF_AUTO_UPDATE, default=saved.get(CONF_AUTO_UPDATE, False)): bool,
            vol.Required(
                CONF_ESPHOME_NEW_DEVICES, default=saved.get(CONF_ESPHOME_NEW_DEVICES, False)
            ): bool,
            vol.Required(
                CONF_HIDE_FOLLOWER_UPDATES, default=saved.get(CONF_HIDE_FOLLOWER_UPDATES, False)
            ): bool,
            vol.Required(CONF_TARGET_VERSION, default=(
                saved_target if saved_target in versions else TARGET_VERSION_LATEST
            )): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=TARGET_VERSION_LATEST, label="Latest"),
                    *(
                        selector.SelectOptionDict(
                            value=v, label=f"{v} (downloaded)" if v in cached else v
                        )
                        for v in versions
                    ),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )),
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
            # KSM-BEHAVE-105: unlike the fields above, these apply to every device.
            vol.Required(CONF_BACKUP_INTERVAL_HOURS, default=saved.get(
                CONF_BACKUP_INTERVAL_HOURS, DEFAULT_BACKUP_INTERVAL_HOURS
            )): vol.All(vol.Coerce(int), vol.Range(min=0, max=8760)),
            vol.Required(CONF_BACKUP_KEEP, default=saved.get(
                CONF_BACKUP_KEEP, DEFAULT_BACKUP_KEEP
            )): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
        }
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields), errors=errors)

    async def async_step_device_menu(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-088: a device entry's Configure is a menu."""
        return self.async_show_menu(
            step_id="device_menu", menu_options=[
                "device_rename", "device_password", "device_host", "device_launcher",
                "device_https", "device_owner",
            ]
        )

    async def async_step_device_https(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-169 (#137): HTTPS is operator opt-in, one device at a time."""
        if not self._entry.data.get(CONF_PASSWORD):
            return self.async_abort(reason="https_password_required")
        if self._entry.data.get(CONF_TLS_SPKI):
            return await self.async_step_device_https_disable()
        return await self.async_step_device_https_enable()

    async def async_step_device_https_enable(self, user_input: dict | None = None) -> FlowResult:
        if user_input is None or not user_input.get("confirm"):
            return self._https_form("device_https_enable", user_input)
        data = self._entry.data
        try:
            pin = await ks_tls.async_establish_tls(
                async_get_clientsession(self.hass), data[CONF_HOST], data[CONF_PASSWORD]
            )
        except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Could not switch %s to HTTPS: %s", data[CONF_HOST], err)
            return self._https_failed(err)
        if pin is None:
            return self.async_abort(reason="https_unsupported")
        return await self._https_saved({**data, CONF_TLS_SPKI: pin}, "https_enabled")

    async def async_step_device_https_disable(self, user_input: dict | None = None) -> FlowResult:
        if user_input is None or not user_input.get("confirm"):
            return self._https_form("device_https_disable", user_input)
        data = self._entry.data
        try:
            await ks_tls.async_disable_tls(
                async_get_clientsession(self.hass), data[CONF_HOST], data[CONF_PASSWORD],
                data[CONF_TLS_SPKI],
            )
        except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Could not switch %s back to HTTP: %s", data[CONF_HOST], err)
            return self._https_failed(err)
        return await self._https_saved(
            {k: v for k, v in data.items() if k != CONF_TLS_SPKI}, "https_disabled"
        )

    def _https_form(self, step_id: str, user_input: dict | None) -> FlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            description_placeholders={"host": self._entry.data[CONF_HOST]},
            errors={"confirm": "confirm_required"} if user_input is not None else {},
        )

    def _https_failed(self, err: Exception) -> FlowResult:
        return self.async_abort(
            reason="https_failed",
            description_placeholders={"reason": str(err) or type(err).__name__},
        )

    async def _https_saved(self, data: dict, reason: str) -> FlowResult:
        """Store the new transport, clear the TLS repairs it resolves, and
        re-poll on it (KSM-BEHAVE-169)."""
        fleet.update_device(self.hass, self._entry, data=data)
        for issue_id in (tls_issue_id(self._entry.entry_id),
                         tls_disabled_issue_id(self._entry.entry_id)):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if coordinator is not None:
            await coordinator.async_request_refresh()
        return self.async_abort(reason=reason)

    async def async_step_device_host(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-130: change where KSM reaches this device, verified under its pin."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input.get(CONF_HOST, "").strip()
            if not host or _HOST_FORBIDDEN.search(host):
                errors[CONF_HOST] = "invalid_host"
            else:
                try:
                    await fetch_health(
                        async_get_clientsession(self.hass), host,
                        pin=self._entry.data.get(CONF_TLS_SPKI),
                    )
                except aiohttp.ServerFingerprintMismatch:
                    errors[CONF_HOST] = "host_key_mismatch"
                except (aiohttp.ClientError, TimeoutError, KsApiError):
                    errors[CONF_HOST] = "cannot_connect"
                else:
                    return self._save_host(host)
        return self.async_show_form(
            step_id="device_host",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=self._entry.data[CONF_HOST]): str,
            }),
            errors=errors,
        )

    def _save_host(self, host: str) -> FlowResult:
        self.hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_HOST: host}
        )
        return self.async_create_entry(title="", data=dict(self._entry.options))

    async def async_step_device_rename(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-127: expose the existing verified rename operation."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input.get(CONF_NAME, "").strip()
            try:
                derive_rename_names(name)
            except ValueError:
                errors[CONF_NAME] = "invalid_device_name"
            if not errors:
                rename = self.hass.data.get(RENAME_API_KEY)
                if rename is None:
                    return self.async_abort(reason="device_rename_unavailable")
                try:
                    result = await rename(self._entry.entry_id, name, allow_adb=True)
                except HomeAssistantError:
                    errors["base"] = "device_rename_failed"
                if errors:
                    return self.async_show_form(
                        step_id="device_rename",
                        data_schema=vol.Schema({
                            vol.Required(CONF_NAME, default=self._entry.title): str,
                        }),
                        errors=errors,
                    )
                parts = [f"{layer}: {result[layer]}" for layer in (
                    "ks", "android", "entry", "host", "esphome"
                )]
                actions = result.get("esphome_actions") or {}
                if actions.get("callers"):
                    parts.append("Old ESPHome action callers: " + ", ".join(actions["callers"]))
                if result.get("error"):
                    parts.append(f"Error: {result['error']}")
                incomplete = any(result[layer] in (
                    "failed", "pending", "unsupported"
                ) for layer in ("ks", "android", "entry", "host", "esphome"))
                return self.async_abort(
                    reason="device_rename_incomplete" if incomplete else "device_rename_done",
                    description_placeholders={"result": "; ".join(parts)},
                )

        return self.async_show_form(
            step_id="device_rename",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default=self._entry.title): str,
            }),
            errors=errors,
        )

    def _launcher_default(self) -> bool:
        try:
            recipe = require_recipe(self._entry.data.get(CONF_DEVICE_PROFILE))
        except NoApprovedRecipe:
            return bool(self._entry.data.get(CONF_REPLACE_LAUNCHER, False))
        return launcher_replacement_wanted(self._entry.data, recipe)

    def _save_replace_launcher(self, value: bool) -> FlowResult:
        self.hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_REPLACE_LAUNCHER: value}
        )
        return self.async_create_entry(title="", data=dict(self._entry.options))

    async def async_step_device_launcher(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-143: whether the next Install makes Kiosk Satellite the Home app."""
        if user_input is not None:
            return self._save_replace_launcher(bool(user_input[CONF_REPLACE_LAUNCHER]))
        return self.async_show_form(
            step_id="device_launcher",
            data_schema=vol.Schema({
                vol.Required(CONF_REPLACE_LAUNCHER, default=self._launcher_default()): bool,
            }),
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
        if self._entry.data.get(CONF_DEVICE_PROFILE) == "portal_gen1":
            return await self.async_step_android9_cleanup(user_input)
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
            meta_error: str | None = None
            try:
                async with support_log.async_run(self.hass, "device_owner", source="configure",
                                                 model_key=model_key, client=client):
                    await client.connect()
                    result = await device_owner.enable_device_owner(client, model_key)
                    if result.meta_setup_needed:
                        meta_error = await _start_meta_setup(self.hass, self._meta_target(), client)
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
            if not result.meta_setup_needed:
                return self.async_abort(reason="device_owner_enabled")
            if meta_error:
                return self.async_abort(
                    reason="device_owner_enabled_meta_failed",
                    description_placeholders={"reason": meta_error},
                )
            return self.async_abort(reason="device_owner_enabled_meta_setup")
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
            if pre.meta_identity_missing:
                return await self.async_step_meta_setup()
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

    async def async_step_android9_cleanup(self, user_input: dict | None = None) -> FlowResult:
        """Gen 1 owner enrollment plus explicitly confirmed Meta cleanup."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if coordinator is not None and getattr(coordinator, "ksm_installing", False):
            return self.async_abort(reason="install_in_progress")
        data = self._entry.data
        host = data[CONF_HOST]
        client = self._owner_client()
        try:
            home = await _native_home_enabled(
                self.hass, host, data.get(CONF_PASSWORD, ""), data.get(CONF_TLS_SPKI),
            )
        except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError, KeyError):
            home = False
        if user_input is not None and user_input.get("confirm"):
            if coordinator is not None:
                coordinator.ksm_installing = True
                coordinator.async_update_listeners()
            try:
                async with support_log.async_run(self.hass, "android9_cleanup", source="configure",
                                                 model_key="portal_gen1", client=client):
                    await client.connect()
                    await device_owner.repurpose_android9_portal(
                        client, "portal_gen1", require_recipe("portal_gen1"),
                        confirmed=True, ks_home_enabled=home,
                    )
            except (AdbAuthPending, AdbConnectFailed, OSError):
                return self.async_abort(
                    reason="adb_unavailable",
                    description_placeholders={"address": f"{host}:{data[CONF_PORT]}"},
                )
            except device_owner.DeviceOwnerError as err:
                _LOGGER.warning("Android 9 cleanup on %s: %s", host, err)
                return self.async_abort(
                    reason="android9_cleanup_failed",
                    description_placeholders={"reason": str(err)},
                )
            finally:
                await client.close()
                if coordinator is not None:
                    coordinator.ksm_installing = False
                    coordinator.async_update_listeners()
            return self.async_abort(reason="android9_cleanup_done")
        try:
            await client.connect()
            await device_owner.android9_cleanup_preflight(
                client, "portal_gen1", require_recipe("portal_gen1"),
                ks_home_enabled=home,
            )
        except (AdbAuthPending, AdbConnectFailed, OSError):
            return self.async_abort(
                reason="adb_unavailable",
                description_placeholders={"address": f"{host}:{data[CONF_PORT]}"},
            )
        except device_owner.DeviceOwnerError as err:
            return self.async_abort(
                reason="android9_cleanup_blocked",
                description_placeholders={"reason": str(err)},
            )
        finally:
            await client.close()
        return self.async_show_form(
            step_id="android9_cleanup",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
        )


    def _meta_target(self) -> "meta_setup.Target":
        data = self._entry.data
        return meta_setup.Target(
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            key_path=data[CONF_KEY_PATH],
            password=data.get(CONF_PASSWORD),
            pin=data.get(CONF_TLS_SPKI),
            model_key=data.get(CONF_DEVICE_PROFILE),
            name=data.get(CONF_NAME) or self._entry.title,
            entry_id=self._entry.entry_id,
        )

    async def async_step_meta_setup(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-113: Kiosk Satellite is already Device Owner but the
        Portal's Meta login is gone -- offer to show Meta's setup screen."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if coordinator is not None and getattr(coordinator, "ksm_installing", False):
            return self.async_abort(reason="install_in_progress")
        address = f"{self._entry.data[CONF_HOST]}:{self._entry.data[CONF_PORT]}"
        errors: dict[str, str] = {}
        if user_input is not None and user_input.get("confirm"):
            client = self._owner_client()
            if coordinator is not None:
                coordinator.ksm_installing = True
                coordinator.async_update_listeners()
            try:
                async with support_log.async_run(
                    self.hass, "meta_setup", source="configure",
                    model_key=self._entry.data.get(CONF_DEVICE_PROFILE), client=client,
                ):
                    await client.connect()
                    error = await _start_meta_setup(self.hass, self._meta_target(), client)
            except (AdbAuthPending, AdbConnectFailed, OSError):
                return self.async_abort(
                    reason="adb_unavailable", description_placeholders={"address": address}
                )
            finally:
                await client.close()
                if coordinator is not None:
                    coordinator.ksm_installing = False
                    coordinator.async_update_listeners()
            if error:
                return self.async_abort(
                    reason="meta_setup_failed", description_placeholders={"reason": error}
                )
            return self.async_abort(reason="meta_setup_started")
        if user_input is not None:
            errors["confirm"] = "confirm_required"
        return self.async_show_form(
            step_id="meta_setup",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )


class KioskSatelliteDeviceSubentryFlow(
    config_entries.ConfigSubentryFlow, KioskSatelliteManagerOptionsFlow
):
    """Expose existing per-device controls on HA's native subentry Configure."""

    def __init__(self) -> None:
        self._entry: fleet.DeviceEntry | None = None

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        return self.async_abort(reason="not_supported")

    async def async_step_reconfigure(self, user_input: dict | None = None) -> FlowResult:
        parent = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        self._entry = fleet.DeviceEntry(self.hass, parent, subentry)
        return await self.async_step_device_menu()

    def _save_host(self, host: str) -> FlowResult:
        return self.async_update_and_abort(
            self._get_entry(), self._get_reconfigure_subentry(),
            data_updates={CONF_HOST: host},
        )

    def _save_replace_launcher(self, value: bool) -> FlowResult:
        return self.async_update_and_abort(
            self._get_entry(), self._get_reconfigure_subentry(),
            data_updates={CONF_REPLACE_LAUNCHER: value},
        )

    async def async_step_device_password(self, user_input: dict | None = None) -> FlowResult:
        """Verify a replacement secret and update the physical subentry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = user_input.get(CONF_PASSWORD, "")
            if not candidate:
                errors[CONF_PASSWORD] = "password_required"
            else:
                try:
                    await login(
                        async_get_clientsession(self.hass),
                        self._entry.data[CONF_HOST], candidate,
                        pin=self._entry.data.get(CONF_TLS_SPKI),
                    )
                except Exception:
                    errors[CONF_PASSWORD] = "password_verification_failed"
                else:
                    return self.async_update_and_abort(
                        self._get_entry(), self._get_reconfigure_subentry(),
                        data_updates={CONF_PASSWORD: candidate},
                    )
        return self.async_show_form(
            step_id="device_password",
            data_schema=vol.Schema({
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }),
            errors=errors,
        )


async def _start_meta_setup(
    hass, target: "meta_setup.Target", client
) -> str | None:
    """Show Meta's setup screen (KSM-BEHAVE-112). Returns None when it is on
    screen and being watched, else why not -- Device Owner is already set,
    so no failure here may be reported as an enrollment failure."""
    try:
        await meta_setup.async_start(hass, target, client)
    except device_owner.DeviceOwnerError as err:
        _LOGGER.warning("Meta setup could not be shown on %s: %s", target.host, err)
        support_log.note(f"meta_setup:{support_log.error_code(err)}")
        return _owner_failure_text(err)
    except Exception as err:  # noqa: BLE001 -- ADB transport drop mid-run
        _LOGGER.warning("Meta setup error on %s: %s", target.host, err)
        support_log.note(f"meta_setup:{support_log.error_code(err)}")
        return "The ADB connection failed while showing the setup screen."
    support_log.note("meta_setup:shown")
    return None


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
    "meta_setup_failed": "Meta's setup screen could not be shown ({detail}).",
    "meta_setup_unsupported": "This Portal model has no known Meta setup app.",
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
    return _OWNER_FAILURES.get(err.code, err.code).format(detail=err.detail)


class KioskSatelliteManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Kiosk Satellite Manager."""

    VERSION = 1

    @classmethod
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        if config_entry.data.get(CONF_ENTRY_TYPE) in (ENTRY_TYPE_UNMANAGED, ENTRY_TYPE_FLEET):
            return {"device": KioskSatelliteDeviceSubentryFlow}
        return {}

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int | None = None
        self._key_path: str | None = None
        self._profile_key: str | None = None
        self._profile_name: str | None = None
        # KSM-BEHAVE-167: an unsupported device's pre-filled support request.
        self._support_url: str | None = None
        self._android_version: str | None = None
        self._discovered_name: str = ""
        self._name: str | None = None
        self._area_id: str | None = None
        self._password: str | None = None
        self._token_mode: str = TOKEN_MODE_AUTO
        self._credential: TokenCredential | None = None
        self._reuse_entry_id: str | None = None
        self._ks_installed: bool = False
        self._existing_install_action: str | None = None
        self._install_task: asyncio.Task | None = None
        self._tls_pin: str | None = None
        self._private_dns_prior: str | None = None
        self._dashboard_dns: DashboardDnsCheck | None = None
        self._ks_probe_pin: str | None = None
        self._global: dict | None = None
        self._ha_url: str | None = None
        self._auto_update: bool = False
        self._want_device_owner: bool = False
        self._want_esphome: bool = False
        self._esphome_task: asyncio.Task | None = None
        self._esphome_outcome: str | None = None
        self._ks_entry: tuple[dict, dict] | None = None
        self._selected_fleet_id: str | None = None
        self._invitation_attempted: bool = False

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return KioskSatelliteManagerOptionsFlow(config_entry)

    async def async_step_import(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-078: auto-create the manager entry from `async_setup`.

        Shares the same unique ID guard as the explicit "Configure KSM"
        choice in `async_step_user`, so this can never create a second
        manager entry alongside one a user already created by hand.
        """
        if user_input and user_input.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_UNMANAGED:
            await self.async_set_unique_id(UNMANAGED_UNIQUE_ID)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Unmanaged", data={CONF_ENTRY_TYPE: ENTRY_TYPE_UNMANAGED}
            )
        if user_input and user_input.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_FLEET:
            leader_id = user_input["leader_id"]
            await self.async_set_unique_id(f"ksm_fleet:{leader_id}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Fleet - {user_input['leader_name']}",
                data={CONF_ENTRY_TYPE: ENTRY_TYPE_FLEET, "leader_id": leader_id,
                      # KSM-BEHAVE-146: offer its roster once, on the first read.
                      **({"offer_followers": True} if user_input.get("offer_followers") else {})},
            )
        await self.async_set_unique_id(MANAGER_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="KSM Settings", data={CONF_ENTRY_TYPE: ENTRY_TYPE_MANAGER}
        )

    async def async_step_integration_discovery(self, discovery_info: dict) -> FlowResult:
        """KSM-BEHAVE-146: a Fleet Manager's roster names a follower KSM does not manage."""
        await self.async_set_unique_id(f"ksm_follower:{discovery_info['ks_id']}")
        self._abort_if_unique_id_configured()
        host = discovery_info["host"]
        if any(device.data.get(CONF_HOST) == host for device in fleet.device_entries(self.hass)):
            return self.async_abort(reason="already_configured")
        self._host = host
        self._discovered_name = discovery_info.get("name") or host
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_follower_confirm()

    async def async_step_follower_confirm(self, user_input: dict | None = None) -> FlowResult:
        """Confirming probes the follower, then joins the existing-KS onboarding."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if any(device.data.get(CONF_HOST) == self._host
                   for device in fleet.device_entries(self.hass)):
                return self.async_abort(reason="already_configured")
            self._global = _manager_options(self.hass)
            ks = await _async_probe_ks_health(async_get_clientsession(self.hass), self._host)
            if ks is not None:
                return await self._async_start_ks_device(self._host, DEFAULT_ADB_PORT, *ks)
            errors["base"] = "cannot_connect_ks"
        return self.async_show_form(
            step_id="follower_confirm", data_schema=vol.Schema({}), errors=errors,
            description_placeholders={"name": self._discovered_name, "host": self._host},
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
                    title="KSM Settings", data={CONF_ENTRY_TYPE: ENTRY_TYPE_MANAGER}
                )
            if not user_input.get(CONF_HOST):
                errors[CONF_HOST] = "host_required"
                return self.async_show_form(
                    step_id="user", data_schema=_onboarding_schema(self.hass), errors=errors
                )
            selected = user_input.get(FLEET_CHOICE, UNMANAGED_CHOICE)
            if selected != UNMANAGED_CHOICE and _invitation_leader(self.hass, selected) is None:
                errors[FLEET_CHOICE] = "fleet_unavailable"
                return self.async_show_form(
                    step_id="user", data_schema=_onboarding_schema(self.hass), errors=errors
                )
            self._selected_fleet_id = selected if selected != UNMANAGED_CHOICE else None
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_ADB_PORT)
            if any(device.data.get(CONF_HOST) == host for device in fleet.device_entries(self.hass)):
                return self.async_abort(reason="already_configured")
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
                    step_id="user", data_schema=_onboarding_schema(self.hass), errors=errors
                )

            try:
                facts = await collect_identity_facts(client)
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
                self._support_url = (
                    None if entry.executable
                    else await support_request.async_url_for_client(self.hass, client)
                )
            finally:
                await client.close()

            self._host = host
            self._port = port
            self._key_path = key_path
            self._profile_key = entry.model_key
            self._profile_name = entry.model_name or entry.classification_name
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
            step_id="user", data_schema=_onboarding_schema(self.hass), errors=errors
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
                "ha_url": self._global.get(CONF_HA_URL, "Home Assistant default"),
                "support_url": self._support_url or "",
            },
        )

    async def async_step_device_info(self, user_input: dict | None = None) -> FlowResult:
        """Collect KSM settings after detection, excluding HA-owned naming."""
        errors: dict[str, str] = {}
        token_mode_options = _token_options(self.hass)

        if user_input is not None:
            password = user_input.get(CONF_PASSWORD) or self._global.get(CONF_PASSWORD, "")
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
                self._token_mode = token_mode
                self._credential = credential
                self._ha_url = ha_url
                self._auto_update = user_input.get(
                    CONF_AUTO_UPDATE, self._global.get(CONF_AUTO_UPDATE, False)
                )
                self._want_device_owner = bool(user_input.get(CONF_ENABLE_DEVICE_OWNER, False))
                self._want_esphome = bool(user_input.get(CONF_ENABLE_ESPHOME, self._esphome_default()))
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
        if self._global:
            fields[vol.Optional(CONF_HA_URL, default=self._global.get(CONF_HA_URL, ""))] = str
            fields[vol.Required(CONF_AUTO_UPDATE, default=self._global.get(CONF_AUTO_UPDATE, False))] = bool
        fields[vol.Required(CONF_ENABLE_ESPHOME, default=self._esphome_default())] = selector.BooleanSelector()
        fields[vol.Required(CONF_ENABLE_DEVICE_OWNER, default=False)] = selector.BooleanSelector()

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
            if self._selected_fleet_id and _invitation_leader(self.hass, self._selected_fleet_id) is None:
                errors["base"] = "fleet_unavailable"
                user_input = None
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            session = async_get_clientsession(self.hass)
            ks_token: str | None = None
            try:
                if self._ks_probe_pin is None:
                    # A freshly installed or reset KS answers health but has no
                    # admin password yet: the submitted one becomes its first.
                    try:
                        status = await ks_api_client.get_setup_status(session, self._host, pin=None)
                    except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError):
                        status = {}
                    if status.get("passwordNeeded"):
                        await ks_api_client.setup_password(
                            session, self._host, password, self._discovered_name, pin=None
                        )
                    # HTTP device: the password check is the login itself.
                    ks_token = await login(session, self._host, password, pin=None)
            except KsApiError:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect_ks"
            # #137: HTTPS is opt-in (KSM-BEHAVE-169). Adopt keeps the device's
            # transport, pinning only a key it already serves.
            pin = self._ks_probe_pin
            if not errors and (pin is not None or ks_token is None):
                try:
                    ks_token = await login(session, self._host, password, pin=pin)
                except KsApiError:
                    errors["base"] = "invalid_auth"
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    errors["base"] = "cannot_connect_ks"
            global_opts = self._global or {}
            if not errors:
                self._password = password
                self._tls_pin = pin
                await self._async_maybe_invite()
                # KSM-BEHAVE-163: point the kiosk at this HA, as the ADB
                # install does; without ADB this is the only path that can.
                try:
                    recipe = require_recipe(self._profile_key)
                except NoApprovedRecipe:
                    recipe = None
                try:
                    self._credential = await async_connect_ha(
                        self.hass,
                        session,
                        self._host,
                        ks_token,
                        pin=pin,
                        recipe=recipe,
                        device_name=self._discovered_name,
                        password=password,
                        ha_url=global_opts.get(CONF_HA_URL),
                    )
                except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError) as err:
                    _LOGGER.warning(
                        "Could not point %s at this Home Assistant: %s",
                        self._host, type(err).__name__,
                    )
                    errors["base"] = "cannot_connect_ks"
            if not errors:
                self._want_esphome = bool(user_input.get(CONF_ENABLE_ESPHOME, self._esphome_default()))
                data = {
                    CONF_HOST: self._host,
                    CONF_PORT: self._port,
                    CONF_KEY_PATH: self._key_path,
                    CONF_DEVICE_PROFILE: self._profile_key,
                    CONF_NAME: self._discovered_name,
                    CONF_AREA_ID: None,
                    CONF_PASSWORD: password,
                    CONF_HA_URL: global_opts.get(CONF_HA_URL),
                    # KSM-BEHAVE-110: copied at creation like auto-update.
                    CONF_ESPHOME_ENABLE_PENDING: self._want_esphome,
                    CONF_TOKEN_MODE: TOKEN_MODE_AUTO,
                    **self._credential.as_entry_data(),
                }
                if pin:
                    data[CONF_TLS_SPKI] = pin
                self._ks_entry = (data, {CONF_AUTO_UPDATE: global_opts.get(CONF_AUTO_UPDATE, False)})
                if self._want_esphome:
                    return await self.async_step_esphome()
                return self._async_create_ks_entry()

        return self.async_show_form(
            step_id="ks_device_info",
            data_schema=vol.Schema({
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_ENABLE_ESPHOME, default=self._esphome_default()): selector.BooleanSelector(),
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
            if self._existing_install_action in (
                EXISTING_INSTALL_REUSE, EXISTING_INSTALL_UPDATE_SETTINGS
            ):
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
                                selector.SelectOptionDict(
                                    value=EXISTING_INSTALL_UPDATE_SETTINGS,
                                    label="Update settings without uninstalling",
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
            self._want_device_owner = bool(user_input.get(CONF_ENABLE_DEVICE_OWNER, False))
            self._want_esphome = bool(user_input.get(CONF_ENABLE_ESPHOME, self._esphome_default()))
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
        fields[vol.Required(CONF_ENABLE_ESPHOME, default=self._esphome_default())] = selector.BooleanSelector()
        fields[vol.Required(CONF_ENABLE_DEVICE_OWNER, default=False)] = selector.BooleanSelector()
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
        if self._selected_fleet_id and _invitation_leader(self.hass, self._selected_fleet_id) is None:
            return self.async_abort(reason="fleet_unavailable")
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
            try:
                async with support_log.async_run(self.hass, "onboarding_install",
                                                 model_key=self._profile_key, client=client):
                    await client.connect()
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
                        device_model=self._profile_key,
                        ha_url=self._ha_url,
                        on_tls_pinned=self._set_tls_pin,
                        on_private_dns_disabled=self._set_private_dns_prior,
                        on_dashboard_dns=self._set_dashboard_dns,
                        before_ha_setup=self._async_maybe_invite,
                        replace_launcher=launcher_replacement_wanted({}, require_recipe(self._profile_key)),
                        fail_on_sync_error=(
                            self._existing_install_action == EXISTING_INSTALL_UPDATE_SETTINGS
                        ),
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
            updating = self._existing_install_action == EXISTING_INSTALL_UPDATE_SETTINGS
            persistent_notification.async_create(
                self.hass,
                message=(
                    f"Kiosk Satellite settings could not be updated on {self._host}. "
                    "The device was added, but its settings may still point to the old "
                    "Home Assistant. Check the existing Web UI password and retry with "
                    "the Install/Reinstall button."
                    if updating else
                    f"Kiosk Satellite could not be installed automatically on "
                    f"{self._host}. The device was still added to Kiosk Satellite "
                    "Manager; use its Install/Reinstall button to retry."
                ),
                title=("Kiosk Satellite settings update failed" if updating else
                       "Kiosk Satellite automatic installation failed"),
                notification_id=f"{DOMAIN}_install_failed_{self._host}",
            )

    def _set_tls_pin(self, pin: str) -> None:
        """KSM-BEHAVE-094: the device key onboarding pinned, stored in the
        created entry."""
        self._tls_pin = pin

    def _set_private_dns_prior(self, prior: str) -> None:
        """KSM-BEHAVE-152: the Private DNS mode install turned off, stored in
        the created entry for uninstall to restore."""
        self._private_dns_prior = prior

    def _set_dashboard_dns(self, check: DashboardDnsCheck) -> None:
        """KSM-BEHAVE-154: the entry does not exist yet; its first setup
        raises or clears the repair under the device's own ID."""
        self._dashboard_dns = check

    async def async_step_install_done(self, user_input: dict | None = None) -> FlowResult:
        # Fresh installs invite from the bootstrap callback. A failed install
        # never reaches it, so must not fall through to an invitation here.
        if self._existing_install_action == EXISTING_INSTALL_REUSE:
            await self._async_maybe_invite()
        if self._want_esphome:
            return await self.async_step_esphome()
        return await self._async_after_esphome()

    async def _async_after_esphome(self) -> FlowResult:
        if self._ks_entry is not None:
            return self._async_create_ks_entry()
        if self._want_device_owner:
            return await self.async_step_onboard_device_owner()
        return self._async_create_device_entry()

    def _esphome_default(self) -> bool:
        return bool((self._global or {}).get(CONF_ESPHOME_NEW_DEVICES, False))

    def _async_create_ks_entry(self) -> FlowResult:
        data, options = self._ks_entry
        return self.async_create_entry(title=self._discovered_name, data=data, options=options)

    async def async_step_esphome(self, user_input: dict | None = None) -> FlowResult:
        """KSM-BEHAVE-135: add the kiosk to HA's ESPHome integration. Best-effort."""
        if not self._esphome_task:
            self._esphome_task = self.hass.async_create_task(self._async_do_esphome())

        if not self._esphome_task.done():
            return self.async_show_progress(
                step_id="esphome",
                progress_action="esphome",
                progress_task=self._esphome_task,
            )

        return self.async_show_progress_done(next_step_id="esphome_done")

    async def _async_do_esphome(self) -> None:
        name = self._name or self._discovered_name
        try:
            outcome = await esphome_adopt.async_adopt(
                self.hass,
                host=self._host,
                name=name,
                password=self._password,
                pin=self._tls_pin,
            )
        except Exception:  # noqa: BLE001 -- best-effort, the entry is still created
            _LOGGER.exception("adding %s to ESPHome failed", self._host)
            outcome = esphome_adopt.FAILED
        self._esphome_outcome = outcome
        if outcome not in (esphome_adopt.ADDED, esphome_adopt.ALREADY_ADDED):
            persistent_notification.async_create(
                self.hass,
                message=(
                    f"{name} could not be added to the ESPHome integration automatically "
                    f"({outcome}). It was still added to Kiosk Satellite Manager. Turn on "
                    "ESPHome in Kiosk Satellite and add the discovered device in Settings > "
                    "Devices & services."
                ),
                title="Kiosk Satellite ESPHome setup incomplete",
                notification_id=f"{DOMAIN}_esphome_failed_{self._host}",
            )

    async def async_step_esphome_done(self, user_input: dict | None = None) -> FlowResult:
        return await self._async_after_esphome()

    async def _async_maybe_invite(self) -> None:
        """Send one invitation after the new KS admin endpoint is reachable."""
        if self._selected_fleet_id is None or self._invitation_attempted:
            return
        self._invitation_attempted = True
        try:
            await _async_invite_to_fleet(
                self.hass, self._selected_fleet_id, self._host, self._password, self._tls_pin,
            )
        except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
            _LOGGER.warning("Fleet invitation failed for %s: %s", self._host, type(err).__name__)
            persistent_notification.async_create(
                self.hass,
                message=(f"Could not invite {self._discovered_name or self._host} to the selected "
                         "Fleet. The device was added to KSM under Unmanaged. "
                         "Use the leader's Fleet Management page to retry."),
                title="Kiosk Satellite Fleet invitation failed",
                notification_id=f"{DOMAIN}_fleet_invite_{self._host}",
            )
        else:
            persistent_notification.async_create(
                self.hass,
                message=(f"The Fleet invitation for {self._discovered_name or self._host} was sent. "
                         "Accept it on the new kiosk; KSM will move the device after KS confirms membership."),
                title="Accept Kiosk Satellite Fleet invitation",
                notification_id=f"{DOMAIN}_fleet_invite_{self._host}",
            )

    async def async_step_onboard_device_owner(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """KSM-BEHAVE-098: the add form's Device Owner opt-in. Same preflight,
        confirmation and enrollment as the device entry's Configure step
        (KSM-BEHAVE-088..090), but every outcome still creates the entry."""
        if self._profile_key == "portal_gen1":
            return await self.async_step_onboard_android9_cleanup(user_input)
        address = f"{self._host}:{self._port}"
        unreachable = (
            f"ADB at {address} could not be reached. Device Owner was not enabled; "
            "use the device's Configure → Enable Device Owner to retry."
        )
        client = AdbClient(self._host, self._port, self._key_path)
        if user_input is not None:
            if not user_input.get("confirm"):
                return self._async_create_device_entry()
            try:
                async with support_log.async_run(self.hass, "device_owner", source="onboarding",
                                                 model_key=self._profile_key, client=client):
                    await client.connect()
                    result = await device_owner.enable_device_owner(client, self._profile_key)
                    meta_error = (
                        await _start_meta_setup(self.hass, self._meta_target(), client)
                        if result.meta_setup_needed
                        else None
                    )
            except (AdbAuthPending, AdbConnectFailed, OSError):
                self._owner_notice(unreachable)
            except device_owner.DeviceOwnerError as err:
                _LOGGER.warning("Device Owner enrollment failed on %s: %s", address, err)
                self._owner_notice(f"Device Owner was not enabled: {_owner_failure_text(err)}")
            except Exception as err:  # noqa: BLE001 -- ADB transport drop mid-run
                _LOGGER.warning("Device Owner enrollment error on %s: %s", address, err)
                self._owner_notice(
                    "Device Owner was not enabled: the ADB connection failed mid-run."
                )
            else:
                if meta_error:
                    self._owner_notice(
                        "Kiosk Satellite is now Device Owner, but the Meta login must be "
                        f"set up again and the setup screen was not shown: {meta_error} "
                        "Use Configure → Enable Device Owner to try again."
                    )
                elif not result.meta_setup_needed:
                    self._owner_notice("Kiosk Satellite is now Device Owner on this device.")
            finally:
                await client.close()
            return self._async_create_device_entry()

        try:
            await client.connect()
            pre = await device_owner.run_preflight(client, self._profile_key)
        except (AdbAuthPending, AdbConnectFailed, OSError):
            self._owner_notice(unreachable)
            return self._async_create_device_entry()
        finally:
            await client.close()

        if device_owner.BLOCKER_ALREADY_OWNER in pre.blockers:
            self._owner_notice(
                "Kiosk Satellite is already Device Owner on this device. Nothing was changed."
            )
            return self._async_create_device_entry()
        if pre.blockers:
            self._owner_notice(
                f"Device Owner cannot be enabled because {_owner_blocker_text(pre)}. "
                "Nothing was changed."
            )
            return self._async_create_device_entry()
        return self.async_show_form(
            step_id="onboard_device_owner",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            description_placeholders={"accounts": _owner_accounts_text(pre)},
        )

    async def async_step_onboard_android9_cleanup(
        self, user_input: dict | None = None,
    ) -> FlowResult:
        """Gen 1 onboarding routes the owner checkbox to the full cleanup."""
        client = AdbClient(self._host, self._port, self._key_path)
        try:
            home = await _native_home_enabled(
                self.hass, self._host, self._password, self._tls_pin,
            )
        except (KsApiError, aiohttp.ClientError, TimeoutError, ValueError, KeyError):
            home = False
        if user_input is not None:
            if not user_input.get("confirm"):
                return self._async_create_device_entry()
            try:
                async with support_log.async_run(self.hass, "android9_cleanup", source="onboarding",
                                                 model_key="portal_gen1", client=client):
                    await client.connect()
                    await device_owner.repurpose_android9_portal(
                        client, "portal_gen1", require_recipe("portal_gen1"),
                        confirmed=True, ks_home_enabled=home,
                    )
            except (AdbAuthPending, AdbConnectFailed, OSError):
                self._owner_notice("Android 9 cleanup failed: network ADB is unavailable.")
            except device_owner.DeviceOwnerError as err:
                self._owner_notice(f"Android 9 cleanup did not finish: {err}")
            else:
                self._owner_notice(
                    "Android 9 cleanup completed; Kiosk Satellite is Device Owner and Home, "
                    "Meta accounts are gone, and network ADB answered."
                )
            finally:
                await client.close()
            return self._async_create_device_entry()
        try:
            await client.connect()
            await device_owner.android9_cleanup_preflight(
                client, "portal_gen1", require_recipe("portal_gen1"),
                ks_home_enabled=home,
            )
        except (AdbAuthPending, AdbConnectFailed, OSError):
            self._owner_notice("Android 9 cleanup was not offered: network ADB is unavailable.")
            return self._async_create_device_entry()
        except device_owner.DeviceOwnerError as err:
            self._owner_notice(f"Android 9 cleanup was not offered: {err}")
            return self._async_create_device_entry()
        finally:
            await client.close()
        return self.async_show_form(
            step_id="onboard_android9_cleanup",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
        )

    def _meta_target(self) -> "meta_setup.Target":
        return meta_setup.Target(
            host=self._host,
            port=self._port,
            key_path=self._key_path,
            password=self._password,
            pin=self._tls_pin,
            model_key=self._profile_key,
            name=self._name or self._host,
        )

    def _owner_notice(self, message: str) -> None:
        persistent_notification.async_create(
            self.hass,
            message=f"{self._name or self._host}: {message}",
            title="Kiosk Satellite Device Owner",
            notification_id=f"{DOMAIN}_device_owner_{self._host}",
        )

    def _async_create_device_entry(self) -> FlowResult:
        data = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_KEY_PATH: self._key_path,
            CONF_DEVICE_PROFILE: self._profile_key,
            CONF_NAME: self._name,
            CONF_AREA_ID: self._area_id,
            CONF_PASSWORD: self._password,
            CONF_HA_URL: self._ha_url,
            CONF_ESPHOME_ENABLE_PENDING: self._want_esphome,
        }
        if self._tls_pin:
            data[CONF_TLS_SPKI] = self._tls_pin
        if self._private_dns_prior is not None:
            data[CONF_PRIVATE_DNS_PRIOR] = self._private_dns_prior
        if pending := meta_setup.take_unowned_pending(self.hass, self._host):
            data[meta_setup.PENDING_KEY] = pending
        if self._dashboard_dns is not None:
            stash_dashboard_dns(self.hass, self._host, self._dashboard_dns)
        if self._existing_install_action != EXISTING_INSTALL_REUSE or self._global:
            data.update(
                {
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
