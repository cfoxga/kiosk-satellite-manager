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
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .adb_client import AdbClient
from .capability_report import CapabilityReportCollector
from .device_catalog import validate_catalog
from .onboarding_plan import build_onboarding_plan
from .const import (
    CONF_HOST,
    CONF_KEY_PATH,
    CONF_PORT,
    DOMAIN,
    HEALTH_SCAN_INTERVAL_MIN,
    PLATFORMS,
    RELEASE_CHECK_INTERVAL_MIN,
    RELEASE_COORDINATOR_KEY,
)
from .credentials import TokenCredential, async_revoke_owned_credential
from .ks_api import latest_release_info
from .provisioning import ProvisioningMismatch, apply_provisioning, fetch_health

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
            return await latest_release_info(session)
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry: start the health-poll coordinator, then the
    button/sensor/switch/update platforms."""
    session = async_get_clientsession(hass)
    host = entry.data[CONF_HOST]

    async def _update():
        return await fetch_health(session, host)

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

    if not hass.services.has_service(DOMAIN, SERVICE_PROVISION):

        async def _handle_provision(call: ServiceCall) -> None:
            target_entry, target_coordinator = _active_target(hass, call.data["config_entry_id"])
            await _authorize_target(call, hass, target_entry)
            client = AdbClient(
                target_entry.data[CONF_HOST],
                target_entry.data[CONF_PORT],
                target_entry.data[CONF_KEY_PATH],
            )
            await client.connect()
            try:
                await apply_provisioning(
                    client,
                    async_get_clientsession(hass),
                    target_entry.data[CONF_HOST],
                    call.data["settings"],
                )
            except ProvisioningMismatch as err:
                raise ServiceValidationError(str(err)) from err
            finally:
                await client.close()
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

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.data.pop(RELEASE_COORDINATOR_KEY, None)
            hass.services.async_remove(DOMAIN, SERVICE_PROVISION)
            hass.services.async_remove(DOMAIN, SERVICE_CAPABILITY_REPORT)
            hass.services.async_remove(DOMAIN, SERVICE_ONBOARDING_PLAN)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Revoke a credential that KSM itself created when its entry is removed."""
    await async_revoke_owned_credential(hass, TokenCredential.from_entry_data(entry.data))
