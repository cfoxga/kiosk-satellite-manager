"""A KSM device without an Area keeps a non-blocking Area repair (KSM-BEHAVE-178, #151)."""
from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import area_registry as ar, device_registry as dr, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import fleet
from custom_components.kiosk_satellite_manager.const import CONF_AREA_ID, CONF_ENTRY_TYPE, DOMAIN
from custom_components.kiosk_satellite_manager.repairs import AreaRequiredFlow

from .test_global_settings import _manager

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_REVOKE_SUBENTRY = "custom_components.kiosk_satellite_manager.credentials.async_revoke_owned_credential"


def _device(ident: str, octet: int, **extra) -> ConfigSubentry:
    return ConfigSubentry(
        data=MappingProxyType({"host": f"192.168.99.{octet}", "port": 5555,
                               "key_path": "/tmp/adbkey", "password": "", **extra}),
        subentry_id=ident, subentry_type="device", title=ident, unique_id=ident,
    )


async def _unmanaged_with(hass, *subentries: ConfigSubentry):
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    for subentry in subentries:
        hass.config_entries.async_add_subentry(unmanaged, subentry)
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        await hass.config_entries.async_reload(unmanaged.entry_id)
        await hass.async_block_till_done()
    return unmanaged


def _issue(hass, ident: str):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"area_required_{ident}")


def _ha_device(hass, ident: str):
    registry = dr.async_get(hass)
    return next(device for entry in hass.config_entries.async_entries(DOMAIN)
                for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
                if (DOMAIN, ident) in device.identifiers)


async def test_KSM_TEST_351_device_created_without_area_raises_repair(hass):
    area = ar.async_get(hass).async_create("Kitchen")
    unmanaged = await _unmanaged_with(
        hass, _device("dev-a", 31), _device("dev-b", 32, **{CONF_AREA_ID: area.id})
    )
    await hass.async_block_till_done()

    assert _ha_device(hass, "dev-a").area_id is None
    issue = _issue(hass, "dev-a")
    assert issue is not None
    assert issue.is_fixable and issue.severity == ir.IssueSeverity.WARNING
    assert _ha_device(hass, "dev-b").area_id == area.id
    assert _issue(hass, "dev-b") is None
    # Grouping and manager entries are never physical devices.
    manager = next(e for e in hass.config_entries.async_entries(DOMAIN)
                   if e.data.get(CONF_ENTRY_TYPE) == "manager")
    assert _issue(hass, manager.entry_id) is None
    assert _issue(hass, unmanaged.entry_id) is None


async def test_KSM_TEST_351_area_set_by_hand_clears_and_removal_reraises(hass):
    area = ar.async_get(hass).async_create("Kitchen")
    await _unmanaged_with(hass, _device("dev-a", 31))
    device = _ha_device(hass, "dev-a")
    assert _issue(hass, "dev-a") is not None

    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    await hass.async_block_till_done()
    assert _issue(hass, "dev-a") is None

    dr.async_get(hass).async_update_device(device.id, area_id=None)
    await hass.async_block_till_done()
    assert _issue(hass, "dev-a") is not None


async def test_KSM_TEST_351_repair_flow_assigns_area_and_clears(hass):
    area = ar.async_get(hass).async_create("Office")
    await _unmanaged_with(hass, _device("dev-a", 31))
    assert _issue(hass, "dev-a") is not None

    flow = AreaRequiredFlow("dev-a")
    flow.hass = hass
    result = await flow.async_step_init({CONF_AREA_ID: area.id})
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert _ha_device(hass, "dev-a").area_id == area.id
    assert fleet.resolve_device(hass, "dev-a").data[CONF_AREA_ID] == area.id
    assert _issue(hass, "dev-a") is None


async def test_KSM_TEST_351_manager_load_sweeps_existing_devices(hass):
    """A device registered before this release (prod's Area-less Portals)
    gets the repair when KSM loads, with no registry event for it."""
    area = ar.async_get(hass).async_create("Den")
    unmanaged = MockConfigEntry(
        domain=DOMAIN, title="Unmanaged", unique_id="ksm_unmanaged",
        data={CONF_ENTRY_TYPE: "unmanaged"},
        subentries_data=[
            {"data": dict(_device("old-a", 41).data), "subentry_id": "old-a",
             "subentry_type": "device", "title": "old-a", "unique_id": "old-a"},
            {"data": dict(_device("old-b", 42).data), "subentry_id": "old-b",
             "subentry_type": "device", "title": "old-b", "unique_id": "old-b"},
        ],
    )
    unmanaged.add_to_hass(hass)
    registry = dr.async_get(hass)
    registry.async_get_or_create(config_entry_id=unmanaged.entry_id, config_subentry_id="old-a",
                                 identifiers={(DOMAIN, "old-a")}, name="old-a")
    old_b = registry.async_get_or_create(config_entry_id=unmanaged.entry_id, config_subentry_id="old-b",
                                         identifiers={(DOMAIN, "old-b")}, name="old-b")
    registry.async_update_device(old_b.id, area_id=area.id)
    await hass.async_block_till_done()
    assert _issue(hass, "old-a") is None  # nothing listening yet: the sweep is what raises it

    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        await _manager(hass)

    assert _issue(hass, "old-a") is not None
    assert _issue(hass, "old-b") is None


async def test_KSM_TEST_351_removing_the_device_clears_the_repair(hass):
    unmanaged = await _unmanaged_with(hass, _device("dev-a", 31))
    assert _issue(hass, "dev-a") is not None

    with patch(_REVOKE_SUBENTRY, new=AsyncMock()), patch(
        _HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})
    ):
        hass.config_entries.async_remove_subentry(unmanaged, "dev-a")
        await hass.async_block_till_done()

    assert _issue(hass, "dev-a") is None
