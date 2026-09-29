"""Offer a Fleet Manager's unmanaged followers (KSM-BEHAVE-146).

Reads only the roster the fleet poll already stored; never writes to KS. A
newly created fleet offers each unmanaged follower as a Discovered card once;
a follower that joins later raises one repair asking whether to add it.
"""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import fleet
from .const import CONF_HOST, DOMAIN

OFFER_PENDING = "offer_followers"
KNOWN_FOLLOWERS = "known_followers"
ISSUE_PREFIX = "new_follower_"


def issue_id(ks_id: str) -> str:
    return f"{ISSUE_PREFIX}{ks_id}"


async def async_offer(hass: HomeAssistant, offer: dict) -> None:
    """Start one Discovered card; HA aborts a duplicate for the same KS ID."""
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={key: offer[key] for key in ("ks_id", "name", "host", "port")},
    )


async def async_sync(hass: HomeAssistant) -> None:
    """Compare each live leader roster with the devices KSM manages."""
    devices = fleet.device_entries(hass)
    managed_ids = {device.fleet_status.get("self_id") for device in devices} - {None}
    managed_hosts = {device.data.get(CONF_HOST) for device in devices}
    registry = ir.async_get(hass)
    for leader in devices:
        status = leader.fleet_status
        rows = status.get("follower_rows")
        if (status.get("leading") is not True or not isinstance(rows, dict)
                or not fleet.status_available(hass, leader.entry_id)):
            continue
        entry = fleet._fleet_entry(hass, status.get("self_id"))
        if entry is None:
            continue
        offers = {
            ks_id: {"ks_id": ks_id, "name": row.get("name") or row["address"],
                    "host": row["address"], "port": row.get("port"),
                    "leader_id": status["self_id"]}
            for ks_id, row in rows.items()
            if isinstance(row.get("address"), str) and row["address"]
            and ks_id not in managed_ids and row["address"] not in managed_hosts
        }
        for (domain, issue), row in list(registry.issues.items()):
            data = row.data or {}
            if (domain == DOMAIN and issue.startswith(ISSUE_PREFIX)
                    and data.get("leader_id") == status["self_id"]
                    and data.get("ks_id") not in offers):
                ir.async_delete_issue(hass, DOMAIN, issue)
        known = entry.data.get(KNOWN_FOLLOWERS)
        if entry.data.get(OFFER_PENDING):
            for offer in offers.values():
                await async_offer(hass, offer)
        elif known is not None:
            for ks_id, offer in offers.items():
                if ks_id not in known:
                    ir.async_create_issue(
                        hass, DOMAIN, issue_id(ks_id), is_fixable=True,
                        severity=ir.IssueSeverity.WARNING, translation_key="new_follower",
                        translation_placeholders={"name": offer["name"],
                                                  "leader": leader.title, "host": offer["host"]},
                        data=offer,
                    )
        recorded = sorted(set(known or ()) | set(rows))
        if recorded != known or OFFER_PENDING in entry.data:
            data = {key: value for key, value in entry.data.items() if key != OFFER_PENDING}
            hass.config_entries.async_update_entry(entry, data={**data, KNOWN_FOLLOWERS: recorded})
