"""Hide follower updates (KSM-BEHAVE-133, #95). ESPHome entities and the
registry are real; only the fleet status a device stores is set directly."""
from __future__ import annotations

from types import MappingProxyType

import pytest
from unittest.mock import AsyncMock, patch
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import follower_updates
from custom_components.kiosk_satellite_manager.const import CONF_ENTRY_TYPE, DOMAIN

from .test_global_settings import _manager

_HIDE = {"hide_follower_updates": True}


@pytest.fixture(autouse=True)
def offline_devices():
    """Fleet parents load with the manager; keep their health and fleetStatus
    polls off the network -- the stored status is set by the tests."""
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.1"})), patch(
        "custom_components.kiosk_satellite_manager.fleet.async_poll_device", new=AsyncMock()
    ):
        yield


def _add_device(hass, parent, sub_id, host, status):
    hass.config_entries.async_add_subentry(parent, ConfigSubentry(
        data=MappingProxyType({
            "host": host, "password": "synthetic-password", "tls_spki": None,
            "_ksm_fleet_status": status,
        }), subentry_id=sub_id, subentry_type="device", title=sub_id, unique_id=sub_id,
    ))


def _fleet(hass):
    parent = MockConfigEntry(domain=DOMAIN, title="Fleet - Lead", data={CONF_ENTRY_TYPE: "fleet", "leader_id": "L"})
    parent.add_to_hass(hass)
    return parent


def _esphome(hass, host, mac, domains=("update",)):
    entry = MockConfigEntry(domain="esphome", unique_id=mac, data={"host": host, "port": 6053, "device_name": mac})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    ids = {}
    for domain in domains:
        ids[domain] = registry.async_get_or_create(
            domain, "esphome", f"{mac}-{domain}", config_entry=entry, suggested_object_id=f"{mac}_{domain}",
        ).entity_id
    return entry, ids


def _disabled(hass, entity_id):
    return er.async_get(hass).async_get(entity_id).disabled_by


async def test_follower_update_is_hidden_and_restored(hass):
    """[KSM-TEST-258] a follower's ESPHome update entity is disabled while the
    option is on; a leader's is not; both reverse when the option goes off."""
    parent = _fleet(hass)
    _add_device(hass, parent, "lead", "192.168.99.10", {"self_id": "L", "leading": True, "observed_at": "x"})
    _add_device(hass, parent, "fol", "192.168.99.11", {"self_id": "F", "leading": False, "following_id": "L", "observed_at": "x"})
    _, lead = _esphome(hass, "192.168.99.10", "aa", domains=("update", "sensor"))
    _, fol = _esphome(hass, "192.168.99.11", "bb", domains=("update", "sensor"))

    manager = await _manager(hass, options=_HIDE)
    await follower_updates.async_sync(hass)
    assert _disabled(hass, fol["update"]) is RegistryEntryDisabler.INTEGRATION
    assert _disabled(hass, lead["update"]) is None
    assert _disabled(hass, fol["sensor"]) is None

    hass.config_entries.async_update_entry(manager, options={})
    await hass.async_block_till_done()
    assert _disabled(hass, fol["update"]) is None


async def test_option_off_hides_nothing(hass):
    """[KSM-TEST-258] negative case: the default (off) disables nothing."""
    parent = _fleet(hass)
    _add_device(hass, parent, "fol", "192.168.99.11", {"self_id": "F", "leading": False, "following_id": "L", "observed_at": "x"})
    _, fol = _esphome(hass, "192.168.99.11", "bb")
    await _manager(hass)
    await follower_updates.async_sync(hass)
    assert _disabled(hass, fol["update"]) is None


async def test_option_turned_on_hides_at_once(hass):
    """[KSM-TEST-258] saving the option applies without a reload."""
    parent = _fleet(hass)
    _add_device(hass, parent, "fol", "192.168.99.11", {"self_id": "F", "leading": False, "following_id": "L", "observed_at": "x"})
    _, fol = _esphome(hass, "192.168.99.11", "bb")
    manager = await _manager(hass)
    hass.config_entries.async_update_entry(manager, options=_HIDE)
    await hass.async_block_till_done()
    assert _disabled(hass, fol["update"]) is RegistryEntryDisabler.INTEGRATION


async def test_promotion_to_leader_restores_the_entity(hass):
    """[KSM-TEST-258] a device that becomes a leader gets its update back."""
    parent = _fleet(hass)
    _add_device(hass, parent, "fol", "192.168.99.11", {"self_id": "F", "leading": False, "following_id": "L", "observed_at": "x"})
    _, fol = _esphome(hass, "192.168.99.11", "bb")
    await _manager(hass, options=_HIDE)
    await follower_updates.async_sync(hass)
    assert _disabled(hass, fol["update"]) is RegistryEntryDisabler.INTEGRATION

    current = parent.subentries["fol"]
    hass.config_entries.async_update_subentry(parent, current, data={
        **current.data, "_ksm_fleet_status": {"self_id": "F", "leading": True, "following_id": None, "observed_at": "y"},
    })
    await follower_updates.async_sync(hass)
    assert _disabled(hass, fol["update"]) is None


async def test_operator_disabled_entity_is_never_reenabled(hass):
    """[KSM-TEST-258] an entity the operator disabled is not recorded, so it
    stays disabled after the option goes on and off."""
    parent = _fleet(hass)
    _add_device(hass, parent, "fol", "192.168.99.11", {"self_id": "F", "leading": False, "following_id": "L", "observed_at": "x"})
    _, fol = _esphome(hass, "192.168.99.11", "bb")
    er.async_get(hass).async_update_entity(fol["update"], disabled_by=RegistryEntryDisabler.USER)
    manager = await _manager(hass, options=_HIDE)
    await follower_updates.async_sync(hass)
    hass.config_entries.async_update_entry(manager, options={})
    await hass.async_block_till_done()
    assert _disabled(hass, fol["update"]) is RegistryEntryDisabler.USER


async def test_integration_disabled_by_esphome_itself_is_never_reenabled(hass):
    """[KSM-TEST-258] a disabled_by=integration KSM did not set is not KSM's to undo."""
    parent = _fleet(hass)
    _add_device(hass, parent, "fol", "192.168.99.11", {"self_id": "F", "leading": False, "following_id": "L", "observed_at": "x"})
    _, fol = _esphome(hass, "192.168.99.11", "bb")
    er.async_get(hass).async_update_entity(fol["update"], disabled_by=RegistryEntryDisabler.INTEGRATION)
    manager = await _manager(hass, options=_HIDE)
    await follower_updates.async_sync(hass)
    hass.config_entries.async_update_entry(manager, options={})
    await hass.async_block_till_done()
    assert _disabled(hass, fol["update"]) is RegistryEntryDisabler.INTEGRATION


async def test_unmatched_and_unmanaged_devices_are_untouched(hass):
    """[KSM-TEST-258] no ESPHome entry at the device's IP, an Unmanaged device,
    and a device with no fleet status read hide nothing."""
    parent = _fleet(hass)
    unmanaged = MockConfigEntry(domain=DOMAIN, title="Unmanaged", data={CONF_ENTRY_TYPE: "unmanaged"})
    unmanaged.add_to_hass(hass)
    _add_device(hass, parent, "noesp", "192.168.99.12", {"self_id": "N", "leading": False, "following_id": "L", "observed_at": "x"})
    _add_device(hass, unmanaged, "free", "192.168.99.13", {"self_id": "U", "leading": False, "following_id": None, "observed_at": "x"})
    _add_device(hass, unmanaged, "blind", "192.168.99.14", {})
    _, other = _esphome(hass, "192.168.99.99", "zz")
    _, free = _esphome(hass, "192.168.99.13", "cc")
    _, blind = _esphome(hass, "192.168.99.14", "dd")
    await _manager(hass, options=_HIDE)
    await follower_updates.async_sync(hass)
    for ids in (other, free, blind):
        assert _disabled(hass, ids["update"]) is None


async def test_late_esphome_update_entity_is_hidden_when_registered(hass):
    """[KSM-TEST-258] the ESPHome entity can register after KSM's sync; the
    registry event hides it."""
    parent = _fleet(hass)
    _add_device(hass, parent, "fol", "192.168.99.11", {"self_id": "F", "leading": False, "following_id": "L", "observed_at": "x"})
    await _manager(hass, options=_HIDE)
    _, fol = _esphome(hass, "192.168.99.11", "bb")
    await hass.async_block_till_done()
    assert _disabled(hass, fol["update"]) is RegistryEntryDisabler.INTEGRATION
