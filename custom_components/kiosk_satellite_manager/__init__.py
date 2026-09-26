"""Kiosk Satellite Manager integration setup.

Phase 1 lands the real setup: a DataUpdateCoordinator polling the device's
/api/health (shared by the version sensor, and refreshed on demand by the
install button), forwarded to the button/sensor platforms.

Phase 2 adds the `kiosk_satellite_manager.provision` service: apply a
settings payload via the device's `ks.provision` ADB intent and fail loudly
if the /api/health read-back doesn't confirm it landed (KSM-BEHAVE-002).

KSM-BEHAVE-006: the coordinator's first refresh must not gate platform
forwarding. A freshly-added device has no Kiosk Satellite app installed yet,
so /api/health is unreachable until the Install button (a platform this
function forwards to) is pressed -- using async_config_entry_first_refresh()
here raised ConfigEntryNotReady on that unreachable health check and aborted
setup before the button entity ever existed, permanently blocking a fresh
device from ever being provisioned. async_refresh() is non-raising: it
records the failure on the coordinator (entities read as unavailable) and
setup still proceeds to create the button/sensor.

KSM-BEHAVE-007: the coordinator carries one extra plain attribute,
ksm_installing, alongside HA's own DataUpdateCoordinator state -- set by the
Install button (and, later, the config flow's auto-install step) for the
duration of an install so the version sensor can show a transitional
"Installing" state instead of "unavailable".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .adb_client import AdbClient
from .capability_report import CapabilityReportCollector
from .device_catalog import validate_catalog
from .onboarding_plan import build_onboarding_plan
from .const import (
    CONF_HOST,
    CONF_ENTRY_TYPE,
    CONF_NAME,
    ENTRY_TYPE_MANAGER,
    CONF_KEY_PATH,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_TLS_SPKI,
    DOMAIN,
    HEALTH_SCAN_INTERVAL_MIN,
    MANAGER_PLATFORMS,
    PLATFORMS,
    RELEASE_CHECK_INTERVAL_MIN,
    RELEASE_COORDINATOR_KEY,
    MANAGER_ENTRY_KEY,
)
from . import ks_tls
from .credentials import TokenCredential, async_revoke_owned_credential
from .ks_api import latest_release_info
from .ks_api_client import KsApiError
from .ks_api_client import login as ks_api_login
from .provisioning import ProvisioningMismatch, apply_provisioning, fetch_health
from .rename import (
    apply_rename_ks_settings,
    derive_dns_host,
    derive_rename_names,
    find_esphome_link,
    rename_esphome_actions,
    resolve_and_verify_dns_host,
    set_android_device_name,
)

_LOGGER = logging.getLogger(__name__)

# KSM-BEHAVE-051 (issue #20): the source catalog is version-controlled data, so
# a malformed edit is a source defect, not a runtime condition. Validate it at
# import so a bad assignment (unknown model, two approved recipes, a launcher
# recipe on launcher-incapable hardware) fails the integration load and the
# gate, instead of surfacing mid-provision on a real device.
validate_catalog()

__all__ = ["DOMAIN", "async_remove_entry", "async_setup_entry", "async_unload_entry"]

SERVICE_PROVISION = "provision"
SERVICE_CAPABILITY_REPORT = "capability_report"
SERVICE_ONBOARDING_PLAN = "onboarding_plan"
SERVICE_RENAME_DEVICE = "rename_device"

# KSM-BEHAVE-068: the device accepts arbitrary settings through its intent,
# but the public HA service does not. Keep this small enough that every key has
# an intentional operator-facing use and an exact runtime type.
PROVISIONING_SETTINGS: dict[str, type] = {
    "device.name": str,
    "device.hostname": str,
    "remote.enabled": bool,
    "remote.password": str,
    "esphome.node_name": str,
}


def _validate_provisioning_settings(settings: dict) -> dict:
    """Reject unknown, empty, and mistyped service settings before ADB work."""
    if not settings:
        raise vol.Invalid("settings must contain at least one supported key")
    for key, value in settings.items():
        expected_type = PROVISIONING_SETTINGS.get(key)
        if expected_type is None:
            raise vol.Invalid(f"unsupported provisioning setting: {key}")
        if type(value) is not expected_type:
            raise vol.Invalid(f"setting {key} must be a {expected_type.__name__}")
        if expected_type is str and not value:
            raise vol.Invalid(f"setting {key} must not be empty")
    return settings

PROVISION_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): str,
        vol.Required("settings"): vol.All(dict, _validate_provisioning_settings),
    }
)

CAPABILITY_REPORT_SCHEMA = vol.Schema({vol.Required("config_entry_id"): str})
ONBOARDING_PLAN_SCHEMA = vol.Schema({vol.Required("config_entry_id"): str})
RENAME_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): str,
        vol.Required("name"): vol.All(str, vol.Length(min=1)),
    }
)


def _active_target(hass: HomeAssistant, config_entry_id: str) -> tuple[ConfigEntry, DataUpdateCoordinator]:
    """Return an active KSM target, rejecting stale config entries before ADB."""
    target_entry = hass.config_entries.async_get_entry(config_entry_id)
    target_coordinator = hass.data.get(DOMAIN, {}).get(config_entry_id)
    if target_entry is None or target_entry.domain != DOMAIN or target_coordinator is None:
        raise ServiceValidationError(f"Unknown or not active {DOMAIN} config entry: {config_entry_id}")
    return target_entry, target_coordinator


async def _authorize_target(call: ServiceCall, hass: HomeAssistant, target_entry: ConfigEntry) -> None:
    """Require an admin or control permission for this entry's action button.

    HA custom-service registration has no target-aware authorization hook. The
    service therefore resolves the caller and the entry's button entity itself,
    before constructing an ADB client. Calls without a human user context
    (including automations) deliberately fail closed.
    """
    user_id = call.context.user_id
    if user_id is None:
        raise ServiceValidationError("Caller is not authorized to manage this KSM device")
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise ServiceValidationError("Caller is not authorized to manage this KSM device")
    if user.is_admin:
        return
    entity_registry = er.async_get(hass)
    target_buttons = (
        entity.entity_id
        for entity in er.async_entries_for_config_entry(entity_registry, target_entry.entry_id)
        if entity.domain == "button"
    )
    if any(user.permissions.check_entity(entity_id, POLICY_CONTROL) for entity_id in target_buttons):
        return
    raise ServiceValidationError("Caller is not authorized to manage this KSM device")


async def _async_ensure_release_coordinator(hass: HomeAssistant) -> None:
    """KSM-BEHAVE-071: create the shared release check on first entry setup.

    Stored before the first await so entries setting up concurrently at
    startup find it instead of each starting their own. Not bound to any
    config entry (config_entry=None): it outlives whichever entry created it.
    """
    if RELEASE_COORDINATOR_KEY in hass.data:
        return
    session = async_get_clientsession(hass)

    async def _update():
        try:
            release = await latest_release_info(session)
            coordinator.ksm_last_success = datetime.now(timezone.utc)
            return release
        except Exception as err:  # network, HTTP status, no usable release
            raise UpdateFailed(f"Kiosk Satellite release check failed: {err}") from err

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=None,
        name=f"{DOMAIN}_release_check",
        update_method=_update,
        update_interval=timedelta(minutes=RELEASE_CHECK_INTERVAL_MIN),
    )
    hass.data[RELEASE_COORDINATOR_KEY] = coordinator
    await coordinator.async_refresh()


def tls_issue_id(entry: ConfigEntry) -> str:
    return f"tls_certificate_changed_{entry.entry_id}"


async def _async_migrate_tls(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """KSM-BEHAVE-094: switch an unpinned device entry to pinned HTTPS once.
    `None` (KS predates TLS) or an error leaves the entry as it was; the
    next setup tries again."""
    host = entry.data[CONF_HOST]
    try:
        pin = await ks_tls.async_establish_tls(
            async_get_clientsession(hass), host, entry.data[CONF_PASSWORD]
        )
    except (KsApiError, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Could not switch Kiosk Satellite %s to HTTPS: %s", host, err)
        return
    if pin is None:
        return
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TLS_SPKI: pin})
    _LOGGER.info("Kiosk Satellite %s now managed over pinned HTTPS", host)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """KSM-BEHAVE-078: ensure the manager entry exists before any entry setup.

    The manager entry owns the only KSM-wide surfaces (release status, Update
    all) -- it is not an optional preference, so an install that only ever
    added device entries through the config flow must still get one. Runs
    once per HA start, via the same unique-ID-guarded creation path the
    explicit "Configure KSM" choice uses, so the two can never race into two
    manager entries.

    Scheduled as a background task rather than awaited here: finishing the
    flow calls back into config-entry setup for this same domain, which would
    deadlock waiting on the setup lock this function's own caller
    (async_setup_component) is still holding open.
    """
    if not any(
        entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT})
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry: start the health-poll coordinator, then the
    button/sensor/switch/update platforms."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
        hass.data[MANAGER_ENTRY_KEY] = entry.entry_id
        await _async_ensure_release_coordinator(hass)
        await hass.config_entries.async_forward_entry_setups(entry, MANAGER_PLATFORMS)
        return True

    session = async_get_clientsession(hass)
    host = entry.data[CONF_HOST]

    async def _update():
        # Read at call time: migration, the Install button and the repair
        # flow all update the pin in place (KSM-BEHAVE-093/095).
        try:
            health = await fetch_health(session, host, pin=entry.data.get(CONF_TLS_SPKI))
        except aiohttp.ServerFingerprintMismatch as err:
            ir.async_create_issue(
                hass,
                DOMAIN,
                tls_issue_id(entry),
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="tls_certificate_changed",
                translation_placeholders={"name": entry.title, "host": host},
                data={"entry_id": entry.entry_id},
            )
            raise UpdateFailed(
                f"{host} presented a TLS key that does not match its pin; "
                "management is blocked until the repair is confirmed"
            ) from err
        return health

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_{host}",
        update_method=_update,
        update_interval=timedelta(minutes=HEALTH_SCAN_INTERVAL_MIN),
    )
    coordinator.ksm_installing = False
    await coordinator.async_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await _async_ensure_release_coordinator(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if entry.data.get(CONF_PASSWORD) and not entry.data.get(CONF_TLS_SPKI):
        entry.async_create_background_task(
            hass, _async_migrate_tls(hass, entry), f"{DOMAIN}_tls_migration_{host}"
        )

    if not hass.services.has_service(DOMAIN, SERVICE_PROVISION):

        async def _handle_provision(call: ServiceCall) -> None:
            """KSM-BEHAVE-083: apply settings over the Kiosk Satellite API
            (`PATCH /api/settings`), never ADB -- superseded 2026-09-24
            (`#47`) from the `ks.provision` ADB intent."""
            target_entry, target_coordinator = _active_target(hass, call.data["config_entry_id"])
            await _authorize_target(call, hass, target_entry)
            password = target_entry.data.get(CONF_PASSWORD)
            if not password:
                raise ServiceValidationError(
                    f"no Kiosk Satellite password stored for {target_entry.title}"
                )
            session = async_get_clientsession(hass)
            host = target_entry.data[CONF_HOST]
            pin = target_entry.data.get(CONF_TLS_SPKI)
            try:
                token = await ks_api_login(session, host, password, pin=pin)
                await apply_provisioning(session, host, token, call.data["settings"], pin=pin)
            except ProvisioningMismatch as err:
                raise ServiceValidationError(str(err)) from err
            except aiohttp.ServerFingerprintMismatch as err:
                raise ServiceValidationError(
                    f"{target_entry.title} presented a TLS key that does not match its pin"
                ) from err
            except KsApiError as err:
                raise ServiceValidationError(
                    f"Kiosk Satellite API error on {target_entry.title}: {err}"
                ) from err
            await target_coordinator.async_request_refresh()

        hass.services.async_register(
            DOMAIN, SERVICE_PROVISION, _handle_provision, schema=PROVISION_SCHEMA
        )

    if not hass.services.has_service(DOMAIN, SERVICE_CAPABILITY_REPORT):

        async def _handle_capability_report(call: ServiceCall) -> dict:
            target_entry, _ = _active_target(hass, call.data["config_entry_id"])
            await _authorize_target(call, hass, target_entry)
            client = AdbClient(
                target_entry.data[CONF_HOST], target_entry.data[CONF_PORT], target_entry.data[CONF_KEY_PATH]
            )
            await client.connect()
            try:
                return {"report": await CapabilityReportCollector(client).collect()}
            finally:
                await client.close()

        hass.services.async_register(
            DOMAIN, SERVICE_CAPABILITY_REPORT, _handle_capability_report,
            schema=CAPABILITY_REPORT_SCHEMA, supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_ONBOARDING_PLAN):

        async def _handle_onboarding_plan(call: ServiceCall) -> dict:
            target_entry, _ = _active_target(hass, call.data["config_entry_id"])
            await _authorize_target(call, hass, target_entry)
            client = AdbClient(
                target_entry.data[CONF_HOST], target_entry.data[CONF_PORT], target_entry.data[CONF_KEY_PATH]
            )
            await client.connect()
            try:
                report = await CapabilityReportCollector(client).collect()
                return {"report": report, "plan": build_onboarding_plan(report)}
            finally:
                await client.close()

        hass.services.async_register(
            DOMAIN, SERVICE_ONBOARDING_PLAN, _handle_onboarding_plan,
            schema=ONBOARDING_PLAN_SCHEMA, supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RENAME_DEVICE):

        async def _handle_rename_device(call: ServiceCall) -> dict:
            """KSM-BEHAVE-084/085: rename a device's KS identity, Android
            system name (when a supported method exists), and KSM entry/DNS
            host. Returns a per-layer result rather than raising once device
            I/O has started, so a partial failure is legible instead of an
            opaque exception."""
            target_entry, target_coordinator = _active_target(hass, call.data["config_entry_id"])
            await _authorize_target(call, hass, target_entry)
            try:
                names = derive_rename_names(call.data["name"])
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            password = target_entry.data.get(CONF_PASSWORD)
            if not password:
                raise ServiceValidationError(
                    f"no Kiosk Satellite password stored for {target_entry.title}"
                )

            session = async_get_clientsession(hass)
            host = target_entry.data[CONF_HOST]
            result: dict = {
                "ks": "failed",
                "android": "unsupported",
                "entry": "failed",
                "host": "unchanged",
                "esphome": "unchanged",
            }
            # KSM-BEHAVE-092: locate the ESPHome entry and snapshot its
            # actions before the PATCH -- HA rewrites the stored node name
            # as soon as KS reconnects under the new one.
            esphome_link = await find_esphome_link(hass, host)
            pin = target_entry.data.get(CONF_TLS_SPKI)
            try:
                token = await ks_api_login(session, host, password, pin=pin)
                result["ks"] = await apply_rename_ks_settings(session, host, token, names, pin=pin)
            except ProvisioningMismatch as err:
                result["error"] = str(err)
                return result
            except aiohttp.ServerFingerprintMismatch:
                result["error"] = f"{target_entry.title} presented a TLS key that does not match its pin"
                return result
            except KsApiError as err:
                result["error"] = f"Kiosk Satellite API error on {target_entry.title}: {err}"
                return result

            result["android"] = await set_android_device_name(names)

            if target_entry.title == names.device_name and target_entry.data.get(CONF_NAME) == names.device_name:
                result["entry"] = "unchanged"
            else:
                hass.config_entries.async_update_entry(
                    target_entry,
                    title=names.device_name,
                    data={**target_entry.data, CONF_NAME: names.device_name},
                )
                result["entry"] = "applied"

            candidate_host = derive_dns_host(host, names.hostname)
            if candidate_host is None or candidate_host == host:
                result["host"] = "unchanged"
            elif await resolve_and_verify_dns_host(hass, session, host, candidate_host, pin=pin):
                hass.config_entries.async_update_entry(
                    target_entry, data={**target_entry.data, CONF_HOST: candidate_host}
                )
                result["host"] = "applied"
            else:
                result["host"] = "pending"

            if esphome_link is None:
                result["esphome"] = "not_found"
            else:
                result["esphome"], esphome_actions = await rename_esphome_actions(
                    hass, esphome_link, names.esphome_node_name
                )
                if esphome_actions is not None:
                    result["esphome_actions"] = esphome_actions

            await target_coordinator.async_request_refresh()
            return result

        hass.services.async_register(
            DOMAIN, SERVICE_RENAME_DEVICE, _handle_rename_device,
            schema=RENAME_DEVICE_SCHEMA, supports_response=SupportsResponse.ONLY,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    manager = entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, MANAGER_PLATFORMS if manager else PLATFORMS
    )
    if unloaded:
        if manager:
            hass.data.pop(MANAGER_ENTRY_KEY, None)
        else:
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN) and MANAGER_ENTRY_KEY not in hass.data:
            hass.data.pop(RELEASE_COORDINATOR_KEY, None)
        if not hass.data.get(DOMAIN):
            for service in (
                SERVICE_PROVISION,
                SERVICE_CAPABILITY_REPORT,
                SERVICE_ONBOARDING_PLAN,
                SERVICE_RENAME_DEVICE,
            ):
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Revoke a credential that KSM itself created when its entry is removed."""
    await async_revoke_owned_credential(hass, TokenCredential.from_entry_data(entry.data))
