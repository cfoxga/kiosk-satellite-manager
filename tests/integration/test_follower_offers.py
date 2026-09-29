"""Offer a Fleet Manager's unmanaged followers (KSM-BEHAVE-146)."""

from types import MappingProxyType
from unittest.mock import ANY, AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigSubentry
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import fleet
from custom_components.kiosk_satellite_manager.const import CONF_ENTRY_TYPE, DOMAIN
from custom_components.kiosk_satellite_manager.repairs import async_create_fix_flow

from .test_global_settings import _manager

LEADER = "ks-leader"
_FLOW = "custom_components.kiosk_satellite_manager.config_flow"


@pytest.fixture(autouse=True)
def _health():
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.95"})):
        yield


def _device(hass, parent, subentry_id, host, title, status=None):
    data = {"host": host, "port": 5555, "key_path": "/tmp/adbkey",
            "password": "synthetic-secret", "tls_spki_sha256": "a" * 64}
    if status:
        data["_ksm_fleet_status"] = status
    hass.config_entries.async_add_subentry(parent, ConfigSubentry(
        data=MappingProxyType(data), subentry_id=subentry_id, subentry_type="device",
        title=title, unique_id=subentry_id,
    ))


def _row(ks_id, name, address, port=2324):
    return {"id": ks_id, "name": name, "address": address, "port": port,
            "phase": "version", "online": True}


def _roster(*rows, followers=True):
    body = {"self": {"id": LEADER}, "leader": True, "following": None}
    if followers:
        body["followers"] = list(rows)
    return {"ok": True, "data": body}


async def _poll(hass, response, commands):
    """Only the leader answers; every other device's read fails (placement kept)."""
    async def command(_session, host, _token, name, *, pin, params=None):
        commands.append(name)
        if host == "192.168.99.10":
            return response
        raise TimeoutError

    with patch("custom_components.kiosk_satellite_manager.ks_api_client.login",
               new=AsyncMock(return_value="synthetic-token")), patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.run_command",
        new=AsyncMock(side_effect=command),
    ):
        await fleet.async_poll_device(hass, "leader-ha")
        await hass.async_block_till_done()


def _offers(hass):
    return {flow["context"]["unique_id"]: flow
            for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            if flow["context"]["source"] == SOURCE_INTEGRATION_DISCOVERY}


def _follower_issues(hass):
    return {issue_id: issue for (domain, issue_id), issue in ir.async_get(hass).issues.items()
            if domain == DOMAIN and issue_id.startswith("new_follower_")}


async def test_new_fleet_offers_each_unmanaged_follower_once(hass):
    """[KSM-TEST-290] A new fleet's roster becomes Discovered cards, once."""
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    _device(hass, unmanaged, "leader-ha", "192.168.99.10", "Hall Kiosk")
    _device(hass, unmanaged, "by-id-ha", "192.168.99.21", "Managed By ID", status={
        "self_id": "ks-by-id", "leading": False, "following_id": LEADER,
        "observed_at": "2026-09-29T00:00:00+00:00",
    })
    _device(hass, unmanaged, "by-host-ha", "192.168.99.30", "Managed By Host")
    roster = _roster(
        _row("ks-mini", "Den Kiosk", "192.168.99.137"),
        _row("ks-by-id", "Managed By ID", "192.168.99.22"),
        _row("ks-by-host", "Managed By Host", "192.168.99.30"),
    )
    commands: list[str] = []

    # An unsupported roster creates the fleet but offers nothing yet.
    await _poll(hass, _roster(followers=False), commands)
    fleet_entry = fleet._fleet_entry(hass, LEADER)
    assert fleet_entry is not None
    assert _offers(hass) == {}
    assert fleet_entry.data.get("offer_followers") is True

    await _poll(hass, roster, commands)
    offers = _offers(hass)
    assert list(offers) == ["ksm_follower:ks-mini"]
    offer = offers["ksm_follower:ks-mini"]
    assert offer["step_id"] == "follower_confirm"
    assert offer["context"]["title_placeholders"] == {"name": "Den Kiosk"}
    fleet_entry = fleet._fleet_entry(hass, LEADER)
    assert sorted(fleet_entry.data["known_followers"]) == ["ks-by-host", "ks-by-id", "ks-mini"]
    assert not fleet_entry.data.get("offer_followers")
    assert _follower_issues(hass) == {}

    # Unreachable on confirm: the card stays and says so.
    with patch(f"{_FLOW}._async_probe_ks_health", new=AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_configure(offer["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "follower_confirm"
    assert result["errors"] == {"base": "cannot_connect_ks"}

    # Confirming continues into the existing-KS password step for that address.
    with patch(f"{_FLOW}._async_probe_ks_health", new=AsyncMock(return_value=(
        "b" * 64, {"name": "Den Kiosk", "appVersion": "2026.9.95"},
    ))) as probe, patch(f"{_FLOW}.ensure_adb_key", return_value="/tmp/adbkey"):
        result = await hass.config_entries.flow.async_configure(offer["flow_id"], {})
    probe.assert_awaited_once_with(ANY, "192.168.99.137")
    assert result["step_id"] == "ks_device_info"
    assert result["description_placeholders"]["name"] == "Den Kiosk"

    # A duplicate offer for the same KS ID never makes a second card.
    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={"ks_id": "ks-mini", "name": "Den Kiosk",
              "host": "192.168.99.137", "port": 2324},
    )
    assert duplicate["type"] is FlowResultType.ABORT
    hass.config_entries.flow.async_abort(offer["flow_id"])

    # Later polls never re-offer a creation-time follower.
    await _poll(hass, roster, commands)
    assert _offers(hass) == {}
    assert _follower_issues(hass) == {}
    assert set(commands) == {"fleetStatus"}


async def test_legacy_fleet_records_roster_without_offering(hass):
    """[KSM-TEST-290] An upgraded fleet without the marker never floods Discovered."""
    await _manager(hass)
    legacy = MockConfigEntry(
        domain=DOMAIN, title="Fleet - Hall Kiosk", unique_id=f"ksm_fleet:{LEADER}",
        data={CONF_ENTRY_TYPE: "fleet", "leader_id": LEADER},
    )
    legacy.add_to_hass(hass)
    _device(hass, legacy, "leader-ha", "192.168.99.10", "Hall Kiosk")
    commands: list[str] = []

    await _poll(hass, _roster(_row("ks-mini", "Den Kiosk", "192.168.99.137")), commands)
    assert _offers(hass) == {}
    assert _follower_issues(hass) == {}
    assert fleet._fleet_entry(hass, LEADER).data["known_followers"] == ["ks-mini"]

    # A failed read changes nothing.
    with patch("custom_components.kiosk_satellite_manager.ks_api_client.login",
               new=AsyncMock(side_effect=TimeoutError)):
        await fleet.async_poll_device(hass, "leader-ha")
    assert fleet._fleet_entry(hass, LEADER).data["known_followers"] == ["ks-mini"]


async def test_follower_joining_later_raises_one_repair(hass):
    """[KSM-TEST-291] A later follower asks through one repair, never twice."""
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    _device(hass, unmanaged, "leader-ha", "192.168.99.10", "Hall Kiosk")
    commands: list[str] = []

    await _poll(hass, _roster(), commands)
    assert fleet._fleet_entry(hass, LEADER).data["known_followers"] == []

    late = _row("ks-late", "Loft Kiosk", "192.168.99.43")
    gone = _row("ks-gone", "Den Kiosk", "192.168.99.137")
    left = _row("ks-left", "Spare Kiosk", "192.168.99.150")
    await _poll(hass, _roster(late, gone, left), commands)
    issues = _follower_issues(hass)
    assert set(issues) == {"new_follower_ks-late", "new_follower_ks-gone", "new_follower_ks-left"}
    assert issues["new_follower_ks-late"].is_fixable
    assert issues["new_follower_ks-late"].translation_placeholders == {
        "name": "Loft Kiosk", "leader": "Hall Kiosk", "host": "192.168.99.43",
    }
    assert _offers(hass) == {}

    # A dismissed repair is never re-raised.
    ir.async_delete_issue(hass, DOMAIN, "new_follower_ks-gone")
    await _poll(hass, _roster(late, gone, left), commands)
    assert set(_follower_issues(hass)) == {"new_follower_ks-late", "new_follower_ks-left"}

    # The fix flow starts the Discovered offer for that follower.
    flow = await async_create_fix_flow(hass, "new_follower_ks-late",
                                       issues["new_follower_ks-late"].data)
    flow.hass = hass
    flow.issue_id = "new_follower_ks-late"
    form = await flow.async_step_init()
    assert form["step_id"] == "confirm"
    done = await flow.async_step_confirm({})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert list(_offers(hass)) == ["ksm_follower:ks-late"]

    # One is managed now (by host) and one left the roster: both repairs go.
    _device(hass, fleet._fleet_entry(hass, LEADER), "late-ha", "192.168.99.43", "Loft Kiosk")
    await _poll(hass, _roster(late, gone), commands)
    assert _follower_issues(hass) == {}
    assert set(commands) == {"fleetStatus"}


async def test_offer_aborts_once_its_host_is_managed(hass):
    """[KSM-TEST-290] A card left open aborts if the follower was added meanwhile."""
    await _manager(hass)
    offer = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={"ks_id": "ks-den", "name": "Den Kiosk", "host": "192.168.99.137", "port": 2324},
    )
    assert offer["step_id"] == "follower_confirm"
    _device(hass, fleet.unmanaged_entry(hass), "den-ha", "192.168.99.137", "Den Kiosk")
    with patch(f"{_FLOW}._async_probe_ks_health", new=AsyncMock()) as probe:
        result = await hass.config_entries.flow.async_configure(offer["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    probe.assert_not_awaited()
    again = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={"ks_id": "ks-den", "name": "Den Kiosk", "host": "192.168.99.137", "port": 2324},
    )
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_configured"


def test_roster_rows_keep_offer_fields():
    """[KSM-TEST-290] The roster keeps each follower's name, address and port."""
    status = fleet._status_from_response(_roster(_row("ks-mini", "Den Kiosk", "192.168.99.137")))
    assert status["follower_rows"]["ks-mini"] == {
        "phase": "version", "online": True, "name": "Den Kiosk",
        "address": "192.168.99.137", "port": 2324,
    }
