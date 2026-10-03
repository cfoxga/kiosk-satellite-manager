"""Repairs that belong to one device (KSM-BEHAVE-154, #126; KSM-BEHAVE-165, #135).

Both are keyed by the device's entry or subentry ID, so they survive an
address change or a fleet move and end when the device leaves KSM.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .adb_client import AdbClient
from .const import CONF_DEVICE_PROFILE, CONF_HOST, CONF_KEY_PATH, CONF_PORT, DOMAIN
from .device_catalog import NoApprovedRecipe, require_recipe, resolve_catalog_entry
from .device_models import collect_identity_facts

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .fleet import DeviceEntry
    from .install import DashboardDnsCheck

_LOGGER = logging.getLogger(__name__)

# Onboarding checks DNS before the device's entry exists; its first setup
# claims the result by host (KSM-BEHAVE-154).
_PENDING_DNS_KEY: Final = f"{DOMAIN}_pending_dashboard_dns"


def tls_issue_id(device_id: str) -> str:
    return f"tls_certificate_changed_{device_id}"


def tls_disabled_issue_id(device_id: str) -> str:
    """KSM-BEHAVE-170: pinned to HTTPS, but the device now serves HTTP."""
    return f"tls_disabled_{device_id}"


def le_certificate_sync_issue_id(device_id: str) -> str:
    return f"le_certificate_sync_failed_{device_id}"


def dashboard_dns_issue_id(device_id: str) -> str:
    return f"dashboard_dns_{device_id}"


def area_required_issue_id(device_id: str) -> str:
    return f"area_required_{device_id}"


def raise_area_required(hass: HomeAssistant, entry: ConfigEntry | DeviceEntry) -> None:
    """KSM-BEHAVE-178: offer an Area picker for a device with no HA Area."""
    ir.async_create_issue(
        hass, DOMAIN, area_required_issue_id(entry.entry_id),
        is_fixable=True, severity=ir.IssueSeverity.WARNING,
        translation_key="area_required",
        translation_placeholders={"name": entry.title},
        data={"entry_id": entry.entry_id},
    )


def sync_area_repair(hass: HomeAssistant, device: dr.DeviceEntry) -> None:
    """KSM-BEHAVE-178: a KSM physical device's HA device without an Area keeps
    the Area repair; one with an Area has none. Manager and grouping entries
    resolve to no physical device, so they never get one."""
    from .fleet import resolve_device  # fleet imports this module

    for domain, ident in device.identifiers:
        if domain != DOMAIN or (entry := resolve_device(hass, ident)) is None:
            continue
        if device.area_id:
            ir.async_delete_issue(hass, DOMAIN, area_required_issue_id(ident))
        else:
            raise_area_required(hass, entry)


def async_track_area_repairs(hass: HomeAssistant) -> Callable[[], None]:
    """KSM-BEHAVE-178: judge every existing device now, then each device the
    registry creates or whose Area changes, so an Area set by hand clears it."""
    registry = dr.async_get(hass)
    devices = {
        device.id: device
        for entry in hass.config_entries.async_entries(DOMAIN)
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
    }
    for device in devices.values():
        sync_area_repair(hass, device)

    @callback
    def _updated(event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] == "update" and "area_id" not in data["changes"]:
            return
        if data["action"] in ("create", "update") and (
            device := registry.async_get(data["device_id"])
        ) is not None:
            sync_area_repair(hass, device)

    return hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _updated)


def device_support_issue_id(device_id: str) -> str:
    return f"device_support_{device_id}"


def factory_reset_issue_id(device_id: str) -> str:
    """KSM-BEHAVE-171: Kiosk Satellite is Device Owner; only a reset removes it."""
    return f"factory_reset_{device_id}"


def raise_factory_reset(hass: HomeAssistant, entry: ConfigEntry | DeviceEntry) -> None:
    """KSM-BEHAVE-171: an explanation and the manual reset steps; KSM has no
    in-app reset (KSM-BEHAVE-172 retired)."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        factory_reset_issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="factory_reset",
        translation_placeholders={"name": entry.title},
        data={"entry_id": entry.entry_id},
    )


# Set on a device entry once its support request was sent (KSM-BEHAVE-165).
SUPPORT_REQUESTED: Final = "support_requested"


def sync_device_support(hass: HomeAssistant, entry: ConfigEntry | DeviceEntry) -> None:
    """KSM-BEHAVE-165: a device KSM cannot provision offers a support request,
    until it has an executable recipe or its request was sent."""
    issue_id = device_support_issue_id(entry.entry_id)
    try:
        require_recipe(entry.data.get(CONF_DEVICE_PROFILE))
        supported = True
    except NoApprovedRecipe:
        supported = False
    if supported or entry.data.get(SUPPORT_REQUESTED):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="device_support",
        translation_placeholders={"name": entry.title},
        data={"entry_id": entry.entry_id},
    )


def needs_model_resolution(entry: ConfigEntry | DeviceEntry) -> bool:
    """KSM-BEHAVE-173: saved with no model and no support request sent."""
    return entry.data.get(CONF_DEVICE_PROFILE) is None and not entry.data.get(SUPPORT_REQUESTED)


async def async_resolve_device_model(hass: HomeAssistant, entry: ConfigEntry | DeviceEntry) -> None:
    """KSM-BEHAVE-173: read a no-model device's identity, store an executable
    match, then judge support. A device that cannot be read is not judged."""
    from .fleet import DeviceEntry, update_device  # fleet imports this module

    client: AdbClient | None = None
    try:
        client = AdbClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_KEY_PATH])
        await client.connect()
        resolution = resolve_catalog_entry(await collect_identity_facts(client))
    except Exception as err:  # noqa: BLE001 -- an unread device is missing evidence
        _LOGGER.debug("%s: model not resolved over ADB: %r", entry.title, err)
        return
    finally:
        if client is not None:
            await client.close()
    if isinstance(entry, DeviceEntry) and not entry.present:
        return
    if resolution.executable:
        update_device(hass, entry, data={**entry.data, CONF_DEVICE_PROFILE: resolution.model_key})
    sync_device_support(hass, entry)


def apply_dashboard_dns(
    hass: HomeAssistant, device_id: str, check: DashboardDnsCheck, device_name: str, host: str
) -> None:
    """KSM-BEHAVE-153: raise the device's repair on a mismatch, clear it once
    the device and HA agree."""
    issue_id = dashboard_dns_issue_id(device_id)
    if not check.mismatch:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    _LOGGER.warning(
        "%s resolves %s to %s, but Home Assistant resolves it to %s",
        host, check.hostname, check.device_address or "nothing", sorted(check.ha_addresses),
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="dashboard_dns_mismatch",
        translation_placeholders={
            "name": device_name,
            "host": host,
            "hostname": check.hostname,
            "device_address": check.device_address or "no address",
            "ha_addresses": ", ".join(sorted(check.ha_addresses)),
        },
    )


def stash_dashboard_dns(hass: HomeAssistant, host: str, check: DashboardDnsCheck) -> None:
    hass.data.setdefault(_PENDING_DNS_KEY, {})[host] = check


def take_dashboard_dns(hass: HomeAssistant, host: str | None) -> DashboardDnsCheck | None:
    if not host:
        return None
    return hass.data.get(_PENDING_DNS_KEY, {}).pop(host, None)


def clear_device_repairs(hass: HomeAssistant, device_id: str) -> None:
    """KSM-BEHAVE-154: the device left KSM; its repairs go with it."""
    for issue_id in (
        tls_issue_id(device_id), tls_disabled_issue_id(device_id),
        le_certificate_sync_issue_id(device_id),
        dashboard_dns_issue_id(device_id), device_support_issue_id(device_id),
        factory_reset_issue_id(device_id), area_required_issue_id(device_id),
    ):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
