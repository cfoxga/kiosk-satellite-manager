"""Kiosk Satellite Manager integration setup.

Phase 1 lands the real setup: a DataUpdateCoordinator polling the device's
/api/health (shared by the version sensor, and refreshed on demand by the
install button), forwarded to the button/sensor platforms.

Phase 2 adds the `kiosk_satellite_manager.provision` service: apply a
settings payload via authenticated `PATCH /api/settings` (KSM-BEHAVE-083).
API per-key failures reject the call; device.name is also verified against
/api/health. Other accepted keys are not independently health-readback verified.

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
import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .adb_client import AdbClient
from .capability_report import CapabilityReportCollector
from .device_catalog import NoApprovedRecipe, require_recipe, validate_catalog
from .install_recipes import NAME_SOURCE_SECURE_BLUETOOTH
from .onboarding_plan import build_onboarding_plan
from .const import (
    BACKUP_CHECK_INTERVAL_MIN,
    CONF_HOST,
    CONF_ENTRY_TYPE,
    CONF_DEVICE_PROFILE,
    CONF_NAME,
    ENTRY_TYPE_MANAGER,
    ENTRY_TYPE_UNMANAGED,
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
    RENAME_API_KEY,
    SIGNAL_MANAGER_OPTIONS_UPDATED,
)
from . import auto_update, config_backup, fleet, follower_updates, ks_tls, meta_setup
from .credentials import TokenCredential, async_revoke_owned_credential
from .ks_api import latest_release_info
from .ks_update import async_check_device_for_update, async_check_devices_for_update
from .esphome_identity import async_ensure_esphome_identity
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

# KSM-BEHAVE-068: the device API exposes more settings than this service,
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
    """Reject unknown, empty, and mistyped service settings before management API work."""
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


def _active_target(hass: HomeAssistant, config_entry_id: str) -> tuple[ConfigEntry | fleet.DeviceEntry, DataUpdateCoordinator]:
    """Return an active KSM target, rejecting stale config entries before ADB."""
    target_entry = fleet.resolve_device(hass, config_entry_id)
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
        for entity in er.async_entries_for_config_entry(
            entity_registry, target_entry.parent.entry_id if isinstance(target_entry, fleet.DeviceEntry)
            else target_entry.entry_id
        )
        if entity.domain == "button" and (
            not isinstance(target_entry, fleet.DeviceEntry)
            or entity.config_subentry_id == target_entry.subentry_id
        )
    )
    if any(user.permissions.check_entity(entity_id, POLICY_CONTROL) for entity_id in target_buttons):
        return
    raise ServiceValidationError("Caller is not authorized to manage this KSM device")


async def _async_rename_entry(
    hass: HomeAssistant, target_entry: ConfigEntry, target_coordinator: DataUpdateCoordinator, name: str,
    *, allow_adb: bool = False,
) -> dict:
    """KSM-BEHAVE-084/085/092: the rename operation, shared by the authorized
    service and the in-process callable (KSM-BEHAVE-102). Returns a per-layer
    result rather than raising once device I/O has started, so a partial
    failure is legible instead of an opaque exception."""
    try:
        names = derive_rename_names(name)
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

    if allow_adb:
        try:
            recipe = require_recipe(target_entry.data.get(CONF_DEVICE_PROFILE))
        except NoApprovedRecipe:
            recipe = None
        if recipe is not None and recipe.device_name_source == NAME_SOURCE_SECURE_BLUETOOTH:
            try:
                client = AdbClient(
                    host, target_entry.data[CONF_PORT], target_entry.data[CONF_KEY_PATH]
                )
                result["android"] = await set_android_device_name(names, client, recipe)
            except Exception:  # noqa: BLE001 -- ADB transport/auth/key errors vary by build
                result["android"] = "failed"
                result["error"] = (
                    "Portal Name could not be verified over ADB. Enable network ADB "
                    "on the Portal and retry Rename device."
                )
        else:
            result["android"] = "unsupported"
    else:
        result["android"] = await set_android_device_name(names)
    if result["android"] == "failed" and "error" not in result:
        result["error"] = "Portal Name did not read back as requested; retry Rename device."

    if target_entry.title == names.device_name and target_entry.data.get(CONF_NAME) == names.device_name:
        result["entry"] = "unchanged"
    else:
        fleet.update_device(
            hass, target_entry,
            title=names.device_name,
            data={**target_entry.data, CONF_NAME: names.device_name},
        )
        result["entry"] = "applied"

    candidate_host = derive_dns_host(host, names.hostname)
    if candidate_host is None or candidate_host == host:
        result["host"] = "unchanged"
    elif await resolve_and_verify_dns_host(hass, session, host, candidate_host, pin=pin):
        fleet.update_device(
            hass, target_entry, data={**target_entry.data, CONF_HOST: candidate_host}
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
    if isinstance(target_entry, fleet.DeviceEntry):
        await fleet.async_reconcile(hass)
    return result


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
        except Exception as err:  # network, HTTP status, no usable release
            raise UpdateFailed(f"Kiosk Satellite release check failed: {err}") from err
        coordinator.ksm_last_success = datetime.now(timezone.utc)
        # KSM-BEHAVE-103/119: a newly seen version tells already loaded
        # devices to check now, including the first successful check. Device
        # entries that load afterward check individually (KSM-BEHAVE-117).
        announced = coordinator.ksm_announced_version
        coordinator.ksm_announced_version = release.version
        if release.version != announced and hass.data.get(DOMAIN):
            hass.async_create_background_task(
                async_check_devices_for_update(hass), f"{DOMAIN}_device_update_check"
            )
        return release

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=None,
        name=f"{DOMAIN}_release_check",
        update_method=_update,
        update_interval=timedelta(minutes=RELEASE_CHECK_INTERVAL_MIN),
    )
    coordinator.ksm_announced_version = None
    hass.data[RELEASE_COORDINATOR_KEY] = coordinator
    await coordinator.async_refresh()

_AUTO_UPDATE_STOPS_KEY = f"{DOMAIN}_auto_update_stops"


def _auto_update_key(device) -> tuple[str, str]:
    """Owning config entry plus device id: a device migrating between parents
    keeps its ID, and the old parent's unload must not stop the new one's."""
    return (getattr(device, "parent", device).entry_id, device.entry_id)


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
    fleet.update_device(hass, entry, data={**entry.data, CONF_TLS_SPKI: pin})
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
    fleet.mark_domain_loading(hass)

    async def _trusted_rename(
        config_entry_id: str, name: str, *, allow_adb: bool = False
    ) -> dict:
        """KSM-BEHAVE-102: in-process only; no caller authorization."""
        target_entry, target_coordinator = _active_target(hass, config_entry_id)
        return await _async_rename_entry(
            hass, target_entry, target_coordinator, name, allow_adb=allow_adb
        )

    hass.data[RENAME_API_KEY] = _trusted_rename

    if not any(
        entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT})
        )
    return True


async def _async_manager_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """KSM-BEHAVE-114: devices re-read the Install version; KSM-BEHAVE-133:
    the Hide follower updates option applies at once."""
    async_dispatcher_send(hass, SIGNAL_MANAGER_OPTIONS_UPDATED)
    await follower_updates.async_sync(hass)


def _device_present(entry: ConfigEntry | fleet.DeviceEntry) -> bool:
    """A plain entry always is; a device subentry until HA removes it."""
    return not isinstance(entry, fleet.DeviceEntry) or entry.present


async def _async_setup_device(hass: HomeAssistant, entry: ConfigEntry | fleet.DeviceEntry) -> None:
    """Start one physical device regardless of its HA parent."""
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
    coordinator.ksm_parent_id = (entry.parent.entry_id if isinstance(entry, fleet.DeviceEntry)
                                 else None)
    await coordinator.async_refresh()
    if not _device_present(entry):
        # Removed during the refresh; its removal reloads the parent (#125).
        await coordinator.async_shutdown()
        return
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    # KSM-BEHAVE-134: KSM adds no update entity or version sensor; drop the
    # legacy rows (these two only) and run auto-update without an entity.
    registry = er.async_get(hass)
    for domain, suffix in (("update", "update"), ("sensor", "version")):
        if legacy := registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{suffix}"):
            registry.async_remove(legacy)
    hass.data.setdefault(_AUTO_UPDATE_STOPS_KEY, {})[_auto_update_key(entry)] = (
        auto_update.async_setup(hass, entry, coordinator)
    )
    if isinstance(entry, fleet.DeviceEntry):
        entry.async_on_unload(coordinator.async_add_listener(
            lambda: hass.async_create_task(fleet.async_poll_device(hass, entry.entry_id))
        ))

    async def _post_setup() -> None:
        if not _device_present(entry):
            return
        if entry.data.get(CONF_PASSWORD) and not entry.data.get(CONF_TLS_SPKI):
            await _async_migrate_tls(hass, entry)
        # KSM-BEHAVE-110 (#67): node name always; ESPHome on only when chosen.
        await async_ensure_esphome_identity(hass, entry)
        # KSM-BEHAVE-117: the shared first check is a baseline and may run
        # before this device is loaded. Refresh its own ESPHome update view.
        try:
            outcome = await async_check_device_for_update(hass, entry, coordinator)
            _LOGGER.info("Kiosk Satellite startup update check on %s: %s", entry.title, outcome)
        except Exception as err:
            _LOGGER.warning("Kiosk Satellite startup update check failed on %s: %s", entry.title, err)

    entry.async_create_background_task(hass, _post_setup(), f"{DOMAIN}_post_setup_{host}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry: start the health-poll coordinator, then the
    button/sensor/switch/update platforms."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
        # Migrate existing manager entries in place, retaining their stable ID.
        if entry.title != "KSM Settings":
            hass.config_entries.async_update_entry(entry, title="KSM Settings")
        if not any(
            other.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_UNMANAGED
            for other in hass.config_entries.async_entries(DOMAIN)
        ):
            hass.async_create_task(hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT},
                data={CONF_ENTRY_TYPE: ENTRY_TYPE_UNMANAGED},
            ))
        hass.data[MANAGER_ENTRY_KEY] = entry.entry_id
        await _async_ensure_release_coordinator(hass)
        entry.async_on_unload(entry.add_update_listener(_async_manager_options_updated))
        entry.async_on_unload(follower_updates.async_setup(hass))
        await hass.config_entries.async_forward_entry_setups(entry, MANAGER_PLATFORMS)
        device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
        if device is not None and device.name != "KSM Settings":
            dr.async_get(hass).async_update_device(device.id, name="KSM Settings")

        async def _backup_tick(_now) -> None:
            # KSM-BEHAVE-105: filename dates decide what is due, not this timer.
            await config_backup.async_run_due_backups(hass)

        entry.async_on_unload(
            async_track_time_interval(
                hass, _backup_tick, timedelta(minutes=BACKUP_CHECK_INTERVAL_MIN),
                name=f"{DOMAIN}_config_backup",
            )
        )
        return True

    devices = (fleet.device_entries(hass, entry) if entry.data.get(CONF_ENTRY_TYPE)
               in (ENTRY_TYPE_UNMANAGED, "fleet") else [entry])
    if entry.data.get(CONF_ENTRY_TYPE) in (ENTRY_TYPE_UNMANAGED, "fleet"):
        fleet.ensure_removal_listener(hass, entry)
    await _async_ensure_release_coordinator(hass)
    for device in devices:
        # Each await below can yield to a concurrent subentry removal (#125).
        if not _device_present(device):
            continue
        await _async_setup_device(hass, device)
        if _device_present(device):
            meta_setup.async_resume(hass, device)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
            host, for an authorized caller."""
            target_entry, target_coordinator = _active_target(hass, call.data["config_entry_id"])
            await _authorize_target(call, hass, target_entry)
            return await _async_rename_entry(hass, target_entry, target_coordinator, call.data["name"])

        hass.services.async_register(
            DOMAIN, SERVICE_RENAME_DEVICE, _handle_rename_device,
            schema=RENAME_DEVICE_SCHEMA, supports_response=SupportsResponse.ONLY,
        )

    if entry.data.get(CONF_ENTRY_TYPE) in (ENTRY_TYPE_UNMANAGED, "fleet"):
        async def _poll_after_setup() -> None:
            await asyncio.sleep(0)
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_UNMANAGED:
                for old in list(hass.config_entries.async_entries(DOMAIN)):
                    if (not old.data.get(CONF_ENTRY_TYPE)
                            and old.state == ConfigEntryState.LOADED):
                        fleet.schedule_migration(hass, old, entry)
            for device in fleet.device_entries(hass, entry):
                await fleet.async_poll_device(hass, device.entry_id)

        hass.async_create_task(_poll_after_setup())

    target = fleet.unmanaged_entry(hass) if not entry.data.get(CONF_ENTRY_TYPE) else None
    if target is not None:
        fleet.schedule_migration(hass, entry, target)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    manager = entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER
    grouping = entry.data.get(CONF_ENTRY_TYPE) in (ENTRY_TYPE_UNMANAGED, "fleet")
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, MANAGER_PLATFORMS if manager else PLATFORMS
    )
    if unloaded:
        stops = hass.data.get(_AUTO_UPDATE_STOPS_KEY, {})
        # By owning entry, not current subentries: a moved or removed device
        # is already gone from the parent when its parent reloads.
        for key in [key for key in stops if key[0] == entry.entry_id]:
            stops.pop(key)()
        if manager:
            hass.data.pop(MANAGER_ENTRY_KEY, None)
        elif grouping:
            # By owning entry too: a removed subentry's coordinator outlives it.
            coordinators = hass.data.get(DOMAIN, {})
            for key in [key for key, item in coordinators.items()
                        if getattr(item, "ksm_parent_id", None) == entry.entry_id]:
                coordinators.pop(key)
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
    if fleet.migration_in_progress(hass, entry.entry_id):
        return
    await async_revoke_owned_credential(hass, TokenCredential.from_entry_data(entry.data))
