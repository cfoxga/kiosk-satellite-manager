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


async def test_KSM_TEST_380_area_follows_unique_esphome_and_music_links(hass):
    """[KSM-TEST-380] A selected or moved Area reaches confirmed linked devices."""
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen").id
    office = areas.async_create("Office").id
    bedroom = areas.async_create("Bedroom").id
    registry = dr.async_get(hass)
    esp_entry = MockConfigEntry(domain="esphome", title="dev-a", data={"host": "192.168.99.31"})
    bluetooth_entry = MockConfigEntry(domain="bluetooth", title="Bluetooth")
    music_entry = MockConfigEntry(domain="music_assistant", title="Music Assistant")
    esp_entry.add_to_hass(hass)
    bluetooth_entry.add_to_hass(hass)
    music_entry.add_to_hass(hass)
    esp = registry.async_get_or_create(config_entry_id=esp_entry.entry_id,
                                       connections={("mac", "aa:bb:cc:dd:ee:01")}, name="dev-a",
                                       manufacturer="kiosk_satellite")
    bluetooth = registry.async_get_or_create(config_entry_id=bluetooth_entry.entry_id,
                                             connections={("bluetooth", "AA:BB:CC:DD:EE:02")},
                                             via_device_id=esp.id, name="dev-a (AA:BB:CC:DD:EE:02)",
                                             manufacturer="esphome")
    music = registry.async_get_or_create(config_entry_id=music_entry.entry_id,
                                         identifiers={("music_assistant", "player-a")}, name="dev-a",
                                         manufacturer="Kiosk Satellite")
    unrelated = registry.async_get_or_create(config_entry_id=music_entry.entry_id,
                                             identifiers={("music_assistant", "other")}, name="dev-b",
                                             manufacturer="Kiosk Satellite")
    assert esp.area_id is None and bluetooth.area_id is None and music.area_id is None

    await _unmanaged_with(hass, _device("dev-a", 31, **{CONF_AREA_ID: kitchen}))
    await hass.async_block_till_done()
    assert registry.async_get(esp.id).area_id == kitchen
    assert registry.async_get(bluetooth.id).area_id == kitchen
    assert registry.async_get(music.id).area_id == kitchen
    assert registry.async_get(unrelated.id).area_id is None

    registry.async_update_device(music.id, area_id=bedroom)
    registry.async_update_device(_ha_device(hass, "dev-a").id, area_id=office)
    await hass.async_block_till_done()
    assert registry.async_get(esp.id).area_id == office
    assert registry.async_get(bluetooth.id).area_id == office
    assert registry.async_get(music.id).area_id == bedroom
    assert fleet.resolve_device(hass, "dev-a").data[CONF_AREA_ID] == office

    registry.async_update_device(_ha_device(hass, "dev-a").id, area_id=None)
    await hass.async_block_till_done()
    assert registry.async_get(esp.id).area_id is None
    assert registry.async_get(bluetooth.id).area_id is None
    assert registry.async_get(music.id).area_id == bedroom
    assert fleet.resolve_device(hass, "dev-a").data[CONF_AREA_ID] is None


async def test_KSM_TEST_380_optional_area_and_ambiguous_music_link(hass):
    """[KSM-TEST-380] No selected Area or duplicate Music Assistant names cause no guesses."""
    kitchen = ar.async_get(hass).async_create("Kitchen").id
    registry = dr.async_get(hass)
    music_entry = MockConfigEntry(domain="music_assistant", title="Music Assistant")
    music_entry.add_to_hass(hass)
    players = [registry.async_get_or_create(
        config_entry_id=music_entry.entry_id,
        identifiers={("music_assistant", f"player-{i}")}, name="dev-a",
        manufacturer="Kiosk Satellite") for i in range(2)]
    esp_devices = []
    for i in range(2):
        esp_entry = MockConfigEntry(domain="esphome", title=f"dev-a-{i}",
                                    data={"host": "192.168.99.31"})
        esp_entry.add_to_hass(hass)
        esp_devices.append(registry.async_get_or_create(
            config_entry_id=esp_entry.entry_id,
            connections={("mac", f"aa:bb:cc:dd:ee:0{i}")}, name=f"dev-a-{i}"))
    await _unmanaged_with(hass, _device("dev-a", 31, **{CONF_AREA_ID: kitchen}),
                          _device("dev-b", 32))
    await hass.async_block_till_done()
    assert _ha_device(hass, "dev-b").area_id is None
    assert all(registry.async_get(player.id).area_id is None for player in players)
    assert all(registry.async_get(device.id).area_id is None for device in esp_devices)


async def test_KSM_TEST_380_late_music_device_gets_area(hass):
    """[KSM-TEST-380] A player and Bluetooth child registered later are reconciled."""
    kitchen = ar.async_get(hass).async_create("Kitchen").id
    await _unmanaged_with(hass, _device("dev-a", 31, **{CONF_AREA_ID: kitchen}))
    music_entry = MockConfigEntry(domain="music_assistant", title="Music Assistant")
    music_entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    music = registry.async_get_or_create(config_entry_id=music_entry.entry_id,
                                         identifiers={("music_assistant", "late-player")},
                                         name="dev-a", manufacturer="Kiosk Satellite")
    assert music.area_id is None, "positive control: creation starts without an Area"
    await hass.async_block_till_done()
    assert registry.async_get(music.id).area_id == kitchen

    esp_entry = MockConfigEntry(domain="esphome", title="dev-a",
                                data={"host": "192.168.99.31"})
    bluetooth_entry = MockConfigEntry(domain="bluetooth", title="Bluetooth")
    esp_entry.add_to_hass(hass)
    bluetooth_entry.add_to_hass(hass)
    esp = registry.async_get_or_create(config_entry_id=esp_entry.entry_id,
                                       connections={("mac", "aa:bb:cc:dd:ee:01")}, name="dev-a")
    await hass.async_block_till_done()
    assert registry.async_get(esp.id).area_id == kitchen
    child = registry.async_get_or_create(config_entry_id=bluetooth_entry.entry_id,
                                         connections={("bluetooth", "AA:BB:CC:DD:EE:02")},
                                         via_device_id=esp.id, name="dev-a (AA:BB:CC:DD:EE:02)",
                                         manufacturer="esphome")
    await hass.async_block_till_done()
    assert registry.async_get(child.id).area_id == kitchen
