"""Repairs that belong to one device (KSM-BEHAVE-154, #126; KSM-BEHAVE-165, #135).

Both are keyed by the device's entry or subentry ID, so they survive an
address change or a fleet move and end when the device leaves KSM.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_DEVICE_PROFILE, DOMAIN
from .device_catalog import NoApprovedRecipe, require_recipe

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


def dashboard_dns_issue_id(device_id: str) -> str:
    return f"dashboard_dns_{device_id}"


def device_support_issue_id(device_id: str) -> str:
    return f"device_support_{device_id}"


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
        dashboard_dns_issue_id(device_id), device_support_issue_id(device_id),
    ):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
