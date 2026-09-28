"""Native fleet entry and subentry behavior (KSM-BEHAVE-120–125)."""

import asyncio
from unittest.mock import AsyncMock, patch
from types import MappingProxyType
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager.const import CONF_ENTRY_TYPE, DOMAIN
from custom_components.kiosk_satellite_manager import fleet
from custom_components.kiosk_satellite_manager import _active_target, _authorize_target
from custom_components.kiosk_satellite_manager.sensor import FleetMembershipSensor, FleetStatusSensor
from custom_components.kiosk_satellite_manager.button import KioskSatelliteUpdateAllButton
from custom_components.kiosk_satellite_manager import config_backup, ks_update
from custom_components.kiosk_satellite_manager.config_flow import KioskSatelliteManagerConfigFlow

from .test_global_settings import _manager


async def test_manager_creates_one_unmanaged_peer_entry(hass):
    """[KSM-TEST-230] Startup creates one stable native Unmanaged peer."""
    manager = await _manager(hass)
    peers = [
        entry for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ENTRY_TYPE) == "unmanaged"
    ]
    assert len(peers) == 1
    assert peers[0].title == "Unmanaged"
    assert peers[0].unique_id == "ksm_unmanaged"
    await hass.config_entries.async_reload(manager.entry_id)
    assert len([
        entry for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ENTRY_TYPE) == "unmanaged"
    ]) == 1


async def test_device_subentry_configure_verifies_password(hass):
    """[KSM-TEST-240] Native device Configure keeps password correction reachable."""
    manager = await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    assert KioskSatelliteManagerConfigFlow.async_get_supported_subentry_types(manager) == {}
    assert "device" in KioskSatelliteManagerConfigFlow.async_get_supported_subentry_types(unmanaged)
    hass.config_entries.async_add_subentry(unmanaged, ConfigSubentry(
        data=MappingProxyType({"host": "192.168.99.12", "port": 5555,
                               "key_path": "/tmp/adbkey", "password": "old"}),
        subentry_id="configure-ha", subentry_type="device", title="Display",
        unique_id="configure-ha",
    ))
    add_flow = await hass.config_entries.subentries.async_init(
        (unmanaged.entry_id, "device"), context={"source": "user"},
    )
    assert add_flow["type"] == "abort"
    flow = await hass.config_entries.subentries.async_init(
        (unmanaged.entry_id, "device"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": "configure-ha"},
    )
    assert flow["type"] == "menu"
    assert "device_owner" in flow["menu_options"]
    form = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"next_step_id": "device_password"},
    )
    assert form["type"] == "form"
    missing = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"password": ""},
    )
    assert missing["errors"]["password"] == "password_required"
    with patch("custom_components.kiosk_satellite_manager.config_flow.login",
               new=AsyncMock(side_effect=RuntimeError("unreachable"))):
        rejected = await hass.config_entries.subentries.async_configure(
            flow["flow_id"], {"password": "wrong"},
        )
    assert rejected["errors"]["password"] == "password_verification_failed"
    assert unmanaged.subentries["configure-ha"].data["password"] == "old"
    with patch("custom_components.kiosk_satellite_manager.config_flow.login",
               new=AsyncMock(return_value="test-token")) as login:
        result = await hass.config_entries.subentries.async_configure(
            flow["flow_id"], {"password": "replacement"},
        )
    assert result["type"] == "abort"
    assert login.await_args.args[1:3] == ("192.168.99.12", "replacement")
    assert unmanaged.subentries["configure-ha"].data["password"] == "replacement"


async def test_legacy_entry_loaded_before_unmanaged_still_migrates(hass):
    """[KSM-TEST-236] Entry setup order cannot strand a legacy device."""
    old = MockConfigEntry(domain=DOMAIN, title="Early Display", data={
        "host": "192.168.99.11", "port": 5555,
        "key_path": "/tmp/adbkey", "password": "",
    })
    old.add_to_hass(hass)
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        assert await hass.config_entries.async_setup(old.entry_id)
        await hass.async_block_till_done()
    unmanaged = fleet.unmanaged_entry(hass)
    assert unmanaged is not None
    assert hass.config_entries.async_get_entry(old.entry_id) is None
    assert old.entry_id in unmanaged.subentries


async def test_existing_entry_migrates_without_registry_or_option_loss(hass):
    """[KSM-TEST-236] Populated registry rows survive entry to subentry."""
    manager = await _manager(hass)
    old = MockConfigEntry(
        domain=DOMAIN, title="Kitchen Display", data={
            "host": "192.168.99.121", "port": 5555, "key_path": "/tmp/adbkey",
            "password": "synthetic-secret", "name": "Kitchen Display",
        }, options={"auto_update": True},
    )
    old.add_to_hass(hass)
    areas = ar.async_get(hass)
    area = areas.async_create("Kitchen")
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=old.entry_id, identifiers={(DOMAIN, old.entry_id)},
        name="Kitchen Display",
    )
    devices.async_update_device(device.id, area_id=area.id)
    entities = er.async_get(hass)
    entity = entities.async_get_or_create(
        "sensor", DOMAIN, f"{old.entry_id}_version", config_entry=old,
        device_id=device.id, suggested_object_id="kitchen_display_version",
    )
    entities.async_update_entity(
        entity.entity_id, name="My display version",
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    with patch("custom_components.kiosk_satellite_manager.ks_api_client.login",
               new=AsyncMock(return_value="test-token")), patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.run_command",
        new=AsyncMock(return_value={"ok": True, "data": {
            "self": {"id": "kitchen-ks"}, "leader": False, "following": None,
        }}),
    ), patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.87"}),
    ):
        assert await hass.config_entries.async_setup(old.entry_id)
        await hass.async_block_till_done()

    unmanaged = next(
        entry for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ENTRY_TYPE) == "unmanaged"
    )
    assert hass.config_entries.async_get_entry(old.entry_id) is None
    assert old.entry_id in unmanaged.subentries
    subentry = unmanaged.subentries[old.entry_id]
    assert subentry.data["password"] == "synthetic-secret"
    assert subentry.data["_ksm_options"] == {"auto_update": True}
    moved_device = devices.async_get(device.id)
    assert (moved_device.config_entry_id, moved_device.config_subentry_id, moved_device.area_id) == (
        unmanaged.entry_id, subentry.subentry_id, area.id,
    )
    moved_entity = entities.async_get(entity.entity_id)
    assert (moved_entity.config_entry_id, moved_entity.config_subentry_id) == (
        unmanaged.entry_id, subentry.subentry_id,
    )
    assert moved_entity.name == "My display version"
    assert moved_entity.disabled_by is er.RegistryEntryDisabler.USER
    assert any(
        item.domain == "button" and item.config_subentry_id == subentry.subentry_id
        for item in er.async_entries_for_config_entry(entities, unmanaged.entry_id)
    )
    managed = fleet.resolve_device(hass, old.entry_id)
    assert isinstance(managed, fleet.DeviceEntry)
    assert fleet.status_available(hass, old.entry_id)
    assert _active_target(hass, old.entry_id)[0].entry_id == old.entry_id
    fleet.update_device(hass, managed, options={**managed.options, "auto_update": False})
    assert unmanaged.subentries[old.entry_id].data["_ksm_options"]["auto_update"] is False
    fleet.update_device(hass, managed, data={**managed.data, "host": "192.168.99.122"})
    assert fleet.resolve_device(hass, old.entry_id).data["host"] == "192.168.99.122"
    assert fleet.resolve_device(hass, old.entry_id).options["auto_update"] is False
    assert (await fleet.async_migrate_legacy_device(hass, old, unmanaged)).subentry_id == old.entry_id
    with patch("custom_components.kiosk_satellite_manager.credentials.async_revoke_owned_credential",
               new=AsyncMock()) as revoke:
        hass.config_entries.async_remove_subentry(unmanaged, old.entry_id)
        await hass.async_block_till_done()
    revoke.assert_awaited_once()
    assert fleet.resolve_device(hass, old.entry_id) is None


async def test_confirmed_membership_moves_without_changing_device_identity(hass):
    """[KSM-TEST-231/232/233] Leader and follower move by stable KS ID."""
    manager = await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    for ident, name in (("leader-ha", "Display"), ("follower-ha", "Display")):
        hass.config_entries.async_add_subentry(unmanaged, ConfigSubentry(
            data=MappingProxyType({"host": f"192.168.99.{21 if ident == 'leader-ha' else 22}",
                                   "port": 5555, "key_path": "/tmp/adbkey", "password": ""}),
            subentry_id=ident, subentry_type="device", title=name, unique_id=ident,
        ))
    devices = dr.async_get(hass)
    registry_device = devices.async_get_or_create(
        config_entry_id=unmanaged.entry_id, config_subentry_id="follower-ha",
        identifiers={(DOMAIN, "follower-ha")}, name="Display",
    )
    leader = unmanaged.subentries["leader-ha"]
    hass.config_entries.async_update_subentry(unmanaged, leader, data={
        **leader.data, "_ksm_fleet_status": {
            "self_id": "ks-leader", "leading": True, "following_id": None,
            "observed_at": "2026-09-28T00:00:00+00:00",
            "pending_invitations": 1,
            "follower_rows": {"ks-follower": {"phase": "version", "online": True}},
        },
    })
    follower = unmanaged.subentries["follower-ha"]
    hass.config_entries.async_update_subentry(unmanaged, follower, data={
        **follower.data, "_ksm_fleet_status": {
            "self_id": "ks-follower", "leading": False, "following_id": "ks-leader",
            "observed_at": "2026-09-28T00:00:00+00:00",
        },
    })
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        await hass.config_entries.async_reload(unmanaged.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    original_entity = next(item for item in er.async_entries_for_config_entry(
        registry, unmanaged.entry_id) if item.unique_id == "follower-ha_version")
    hass.data.setdefault(fleet._READ_OK_KEY, set()).add("leader-ha")
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.87"})), patch(
        "custom_components.kiosk_satellite_manager.credentials.async_revoke_owned_credential",
        new=AsyncMock(),
    ) as revoke:
        await fleet.async_reconcile(hass)
        await hass.async_block_till_done()
    revoke.assert_not_awaited()
    fleets = [item for item in hass.config_entries.async_entries(DOMAIN)
              if item.data.get(CONF_ENTRY_TYPE) == "fleet"]
    assert len(fleets) == 1
    assert fleets[0].title == "Fleet - Display"
    assert set(fleets[0].subentries) == {"leader-ha", "follower-ha"}
    assert devices.async_get(registry_device.id).config_entry_id == fleets[0].entry_id
    assert devices.async_get(registry_device.id).config_subentry_id == "follower-ha"
    assert registry.async_get(original_entity.entity_id).config_entry_id == fleets[0].entry_id
    assert registry.async_get(original_entity.entity_id).config_subentry_id == "follower-ha"
    assert FleetStatusSensor(hass, fleets[0], "leader").native_value == "Display"
    assert FleetStatusSensor(hass, fleets[0], "managed_count").native_value == 2
    assert FleetStatusSensor(hass, fleets[0], "last_poll").native_value == "2026-09-28T00:00:00+00:00"
    assert FleetStatusSensor(hass, fleets[0], "blocked_sync").native_value == 1
    hass.data.setdefault(fleet._READ_OK_KEY, set()).update({"leader-ha", "follower-ha"})
    assert FleetStatusSensor(hass, fleets[0], "online_count").native_value == 2
    assert FleetStatusSensor(hass, fleets[0], "offline_count").native_value == 0
    assert FleetStatusSensor(hass, fleets[0], "pending_invitations").native_value == 1
    assert FleetStatusSensor(hass, fleets[0], "version_mismatch").native_value == 1
    assert FleetStatusSensor(hass, fleets[0], "blocked_sync").native_value == 1
    leader_subentry = fleets[0].subentries["leader-ha"]
    original_leader_data = dict(leader_subentry.data)
    hass.config_entries.async_update_subentry(fleets[0], leader_subentry, data={
        **leader_subentry.data, "_ksm_fleet_status": {
            **leader_subentry.data["_ksm_fleet_status"], "follower_rows": {},
        },
    })
    assert FleetStatusSensor(hass, fleets[0], "version_mismatch").native_value is None
    assert FleetStatusSensor(hass, fleets[0], "blocked_sync").native_value is None
    hass.config_entries.async_update_subentry(
        fleets[0], fleets[0].subentries["leader-ha"], data=original_leader_data
    )
    assert FleetMembershipSensor(hass, fleet.resolve_device(hass, "leader-ha")).native_value == "leader"
    assert FleetMembershipSensor(hass, fleet.resolve_device(hass, "follower-ha")).native_value == "following"
    await fleet._move_device(hass, fleet.resolve_device(hass, "leader-ha"), fleets[0])
    assert set(fleets[0].subentries) == {"leader-ha", "follower-ha"}
    leader_health = hass.data[DOMAIN].pop("leader-ha")
    assert FleetStatusSensor(hass, fleets[0], "blocked_sync").native_value is None
    hass.data[DOMAIN]["leader-ha"] = leader_health
    update_all = KioskSatelliteUpdateAllButton(hass, manager)
    reason = update_all._eligibility(fleet.resolve_device(hass, "follower-ha"), "2026.9.1")
    assert reason != "update entity unavailable"
    with patch("custom_components.kiosk_satellite_manager.ks_update.async_check_device_for_update",
               new=AsyncMock(return_value="checked")) as check:
        outcomes = await ks_update.async_check_devices_for_update(hass)
    assert outcomes == {"Display": "checked"}
    assert {call.args[1].entry_id for call in check.await_args_list} == {
        "leader-ha", "follower-ha",
    }
    with patch("custom_components.kiosk_satellite_manager.config_backup.list_backups",
               return_value=[]), patch(
        "custom_components.kiosk_satellite_manager.config_backup.async_backup_entry",
        new=AsyncMock(),
    ) as backup:
        await config_backup.async_run_due_backups(hass)
    assert {call.args[1].entry_id for call in backup.await_args_list} == {
        "leader-ha", "follower-ha",
    }
    registry = er.async_get(hass)
    leader_buttons = {item.entity_id for item in er.async_entries_for_config_entry(
        registry, fleets[0].entry_id) if item.domain == "button"
        and item.config_subentry_id == "leader-ha"}
    assert leader_buttons
    follower = fleet.resolve_device(hass, "follower-ha")
    user = SimpleNamespace(is_admin=False, permissions=SimpleNamespace(
        check_entity=lambda entity_id, policy: entity_id in leader_buttons,
    ))
    call = SimpleNamespace(context=SimpleNamespace(user_id="operator"))
    with patch.object(hass.auth, "async_get_user", new=AsyncMock(return_value=user)):
        with pytest.raises(ServiceValidationError, match="not authorized"):
            await _authorize_target(call, hass, follower)

    second = ConfigSubentry(
        data=MappingProxyType({
            "host": "192.168.99.23", "port": 5555, "key_path": "/tmp/adbkey", "password": "",
            "_ksm_fleet_status": {"self_id": "ks-other", "leading": True,
                                  "following_id": None,
                                  "observed_at": "2026-09-28T00:00:00+00:00"},
        }),
        subentry_id="other-ha", subentry_type="device", title="Display", unique_id="other-ha",
    )
    hass.config_entries.async_add_subentry(unmanaged, second)
    hass.data.setdefault(fleet._READ_OK_KEY, set()).add("other-ha")
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        await fleet.async_reconcile(hass)
        await hass.async_block_till_done()
    fleets = [item for item in hass.config_entries.async_entries(DOMAIN)
              if item.data.get(CONF_ENTRY_TYPE) == "fleet"]
    assert len(fleets) == 2
    assert len({item.unique_id for item in fleets}) == 2
    original_fleet_id = fleets[0].entry_id
    fleet.update_device(hass, fleet.resolve_device(hass, "leader-ha"), title="Kitchen Display")
    await fleet.async_reconcile(hass)
    assert hass.config_entries.async_get_entry(original_fleet_id).title == "Fleet - Kitchen Display"
    assert any(item.title == "Fleet - Display" for item in fleets)
    await fleet.async_reconcile(hass)
    assert len([item for item in hass.config_entries.async_entries(DOMAIN)
                if item.data.get(CONF_ENTRY_TYPE) == "fleet"]) == 2

    first_fleet = hass.config_entries.async_get_entry(original_fleet_id)
    follower = first_fleet.subentries["follower-ha"]
    hass.config_entries.async_update_subentry(first_fleet, follower, data={
        **follower.data, "_ksm_fleet_status": {
            "self_id": "ks-follower", "leading": False, "following_id": None,
            "observed_at": "2026-09-28T00:01:00+00:00",
        },
    })
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        await fleet.async_reconcile(hass)
        await hass.async_block_till_done()
    assert "follower-ha" in unmanaged.subentries
    assert devices.async_get(registry_device.id).config_entry_id == unmanaged.entry_id
    assert registry.async_get(original_entity.entity_id).config_entry_id == unmanaged.entry_id
    assert FleetMembershipSensor(hass, fleet.resolve_device(hass, "follower-ha")).native_value == "unmanaged"

    first_fleet = hass.config_entries.async_get_entry(original_fleet_id)
    leader = first_fleet.subentries["leader-ha"]
    hass.config_entries.async_update_subentry(first_fleet, leader, data={
        **leader.data, "_ksm_fleet_status": {
            "self_id": "ks-leader", "leading": False, "following_id": None,
            "observed_at": "2026-09-28T00:02:00+00:00",
        },
    })
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        await fleet.async_reconcile(hass)
        await hass.async_block_till_done()
    assert "leader-ha" in unmanaged.subentries
    assert hass.config_entries.async_get_entry(original_fleet_id) is None


async def test_failed_migration_rolls_back_and_can_retry(hass):
    """[KSM-TEST-237] A registry failure retains the old working owner."""
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    old = MockConfigEntry(domain=DOMAIN, title="Office", data={
        "host": "192.168.99.31", "port": 5555, "key_path": "/tmp/adbkey", "password": "",
    })
    old.add_to_hass(hass)
    devices = dr.async_get(hass)
    row = devices.async_get_or_create(config_entry_id=old.entry_id,
                                      identifiers={(DOMAIN, old.entry_id)})
    entities = er.async_get(hass)
    entity = entities.async_get_or_create("sensor", DOMAIN, f"{old.entry_id}_version",
                                          config_entry=old, device_id=row.id)
    with patch.object(devices, "async_update_device", side_effect=RuntimeError("injected")):
        with pytest.raises(RuntimeError, match="injected"):
            await fleet.async_migrate_legacy_device(hass, old, unmanaged)
    assert hass.config_entries.async_get_entry(old.entry_id) is old
    assert old.entry_id not in unmanaged.subentries
    assert entities.async_get(entity.entity_id).config_entry_id == old.entry_id
    assert devices.async_get(row.id).config_entry_id == old.entry_id
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        await fleet.async_migrate_legacy_device(hass, old, unmanaged)
        await hass.async_block_till_done()
    assert old.entry_id in unmanaged.subentries
    assert entities.async_get(entity.entity_id).config_entry_id == unmanaged.entry_id
    assert devices.async_get(row.id).config_entry_id == unmanaged.entry_id


async def test_failed_fleet_move_rolls_back_and_retries(hass):
    """[KSM-TEST-233/237] A failed move leaves the subentry and rows owned once."""
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    for ident, status in (
        ("leader-move", {"self_id": "leader-ks", "leading": True,
                         "following_id": None, "observed_at": "2026-09-28T00:00:00Z"}),
        ("follower-move", {"self_id": "follower-ks", "leading": False,
                           "following_id": "leader-ks", "observed_at": "2026-09-28T00:00:00Z"}),
    ):
        hass.config_entries.async_add_subentry(unmanaged, ConfigSubentry(
            data=MappingProxyType({"host": "192.168.99.51", "port": 5555,
                                   "key_path": "/tmp/adbkey", "password": "",
                                   "_ksm_fleet_status": status}),
            subentry_id=ident, subentry_type="device", title=ident, unique_id=ident,
        ))
    devices = dr.async_get(hass)
    row = devices.async_get_or_create(config_entry_id=unmanaged.entry_id,
                                      config_subentry_id="follower-move",
                                      identifiers={(DOMAIN, "follower-move")})
    entities = er.async_get(hass)
    entity = entities.async_get_or_create("sensor", DOMAIN, "follower-move_version",
                                          config_entry=unmanaged, device_id=row.id,
                                          config_subentry_id="follower-move")
    hass.data.setdefault(fleet._READ_OK_KEY, set()).add("leader-move")
    with patch.object(devices, "async_update_device", side_effect=RuntimeError("injected")):
        with patch("custom_components.kiosk_satellite_manager.fetch_health",
                   new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
            with pytest.raises(RuntimeError, match="injected"):
                await fleet.async_reconcile(hass)
    assert "follower-move" in unmanaged.subentries
    assert devices.async_get(row.id).config_entry_id == unmanaged.entry_id
    assert entities.async_get(entity.entity_id).config_entry_id == unmanaged.entry_id
    destination = next(item for item in hass.config_entries.async_entries(DOMAIN)
                       if item.data.get(CONF_ENTRY_TYPE) == "fleet")
    assert "follower-move" not in destination.subentries
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        await fleet.async_reconcile(hass)
        await hass.async_block_till_done()
    assert "follower-move" in destination.subentries
    assert devices.async_get(row.id).config_entry_id == destination.entry_id


async def test_move_waits_for_parent_platform_setup(hass):
    """[KSM-TEST-233/242] A startup move cannot invalidate setup's device view."""
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    hass.config_entries.async_add_subentry(unmanaged, ConfigSubentry(
        data=MappingProxyType({"host": "192.168.99.71", "port": 5555,
                               "key_path": "/tmp/adbkey", "password": "",
                               "_ksm_fleet_status": {"self_id": "race-leader",
                                                     "leading": True, "following_id": None,
                                                     "observed_at": "2026-09-28T00:00:00Z"}}),
        subentry_id="race-ha", subentry_type="device", title="Race Display",
        unique_id="race-ha",
    ))
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_health(session, host, *, pin):
        started.set()
        await release.wait()
        return {"appVersion": "2026.9.87"}

    with patch("custom_components.kiosk_satellite_manager.fetch_health", side_effect=slow_health):
        setup = hass.async_create_task(hass.config_entries.async_reload(unmanaged.entry_id))
        await started.wait()
        hass.data.setdefault(fleet._READ_OK_KEY, set()).add("race-ha")
        move = hass.async_create_task(fleet.async_reconcile(hass))
        await asyncio.sleep(0)
        assert "race-ha" in unmanaged.subentries
        release.set()
        await asyncio.gather(setup, move)
        await hass.async_block_till_done()
    assert fleet.resolve_device(hass, "race-ha").parent.data[CONF_ENTRY_TYPE] == "fleet"


async def test_duplicate_ks_identity_never_assigns_two_devices_to_one_leader(hass):
    """[KSM-TEST-234] Conflicting immutable IDs leave both placements intact."""
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    for ident in ("collision-a", "collision-b"):
        hass.config_entries.async_add_subentry(unmanaged, ConfigSubentry(
            data=MappingProxyType({"host": "192.168.99.61", "port": 5555,
                                   "key_path": "/tmp/adbkey", "password": "",
                                   "_ksm_fleet_status": {"self_id": "same-ks-id",
                                                         "leading": True, "following_id": None,
                                                         "observed_at": "2026-09-28T00:00:00Z"}}),
            subentry_id=ident, subentry_type="device", title=ident, unique_id=ident,
        ))
    await fleet.async_reconcile(hass)
    assert set(unmanaged.subentries) == {"collision-a", "collision-b"}
    assert not any(item.data.get(CONF_ENTRY_TYPE) == "fleet"
                   for item in hass.config_entries.async_entries(DOMAIN))


async def test_stale_leader_status_does_not_create_fleet(hass):
    """[KSM-TEST-234/241] A failed read cannot turn persisted status into a new fleet."""
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    hass.config_entries.async_add_subentry(unmanaged, ConfigSubentry(
        data=MappingProxyType({"host": "192.168.99.72", "port": 5555,
                               "key_path": "/tmp/adbkey", "password": "",
                               "_ksm_fleet_status": {"self_id": "old-leader",
                                                     "leading": True, "following_id": None,
                                                     "observed_at": "2026-09-28T00:00:00Z"}}),
        subentry_id="stale-ha", subentry_type="device", title="Stale Display",
        unique_id="stale-ha",
    ))
    await fleet.async_reconcile(hass)
    assert fleet._fleet_entry(hass, "old-leader") is None
    hass.data.setdefault(fleet._READ_OK_KEY, set()).add("stale-ha")
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        await fleet.async_reconcile(hass)
        await hass.async_block_till_done()
    assert fleet._fleet_entry(hass, "old-leader") is not None


async def test_authenticated_fleet_read_keeps_external_and_failed_status_unmanaged(hass):
    """[KSM-TEST-234/241] Only valid authenticated self status changes placement."""
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    hass.config_entries.async_add_subentry(unmanaged, ConfigSubentry(
        data=MappingProxyType({"host": "192.168.99.41", "port": 5555,
                               "key_path": "/tmp/adbkey", "password": "synthetic-secret",
                               "tls_spki_sha256": "a" * 64}),
        subentry_id="external-ha", subentry_type="device", title="Guest Display",
        unique_id="external-ha",
    ))
    good = {"ok": True, "data": {
        "self": {"id": "guest-ks"}, "leader": False,
        "following": {"leader": {"id": "not-managed"}},
        "invite": {"leader": {"id": "another-id"}},
    }}
    with patch("custom_components.kiosk_satellite_manager.ks_api_client.login",
               new=AsyncMock(return_value="synthetic-token")) as login, patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.run_command",
        new=AsyncMock(return_value=good),
    ) as command:
        await fleet.async_poll_device(hass, "external-ha")
    login.assert_awaited_once_with(login.call_args.args[0], "192.168.99.41",
                                  "synthetic-secret", pin="a" * 64)
    command.assert_awaited_once_with(command.call_args.args[0], "192.168.99.41",
                                     "synthetic-token", "fleetStatus", pin="a" * 64)
    assert fleet.resolve_device(hass, "external-ha").parent.entry_id == unmanaged.entry_id
    assert fleet.status_available(hass, "external-ha")
    assert FleetMembershipSensor(hass, fleet.resolve_device(hass, "external-ha")).native_value == "external leader"

    with patch("custom_components.kiosk_satellite_manager.ks_api_client.login",
               new=AsyncMock(return_value="synthetic-token")), patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.run_command",
        new=AsyncMock(return_value={"ok": True, "data": {"leader": False}}),
    ):
        await fleet.async_poll_device(hass, "external-ha")
    assert fleet.resolve_device(hass, "external-ha").parent.entry_id == unmanaged.entry_id
    assert not fleet.status_available(hass, "external-ha")
    assert FleetMembershipSensor(hass, fleet.resolve_device(hass, "external-ha")).native_value == "stale"
    assert fleet.resolve_device(hass, "external-ha").fleet_status["following_id"] == "not-managed"
    with pytest.raises(ValueError, match="contradict"):
        fleet._status_from_response({"ok": True, "data": {
            "self": {"id": "guest-ks"}, "leader": True,
            "following": {"leader": {"id": "not-managed"}},
        }})


def test_status_summary_only_counts_known_fields():
    """[KSM-TEST-235] Missing roster data remains unknown, not healthy."""
    base = {"ok": True, "data": {"self": {"id": "ks-leader"},
                                  "leader": True, "following": None}}
    unknown = fleet._status_from_response(base)
    assert unknown["follower_rows"] is None
    assert unknown["pending_invitations"] is None
    known = fleet._status_from_response({"ok": True, "data": {
        **base["data"], "followers": [
            {"id": "managed-a", "phase": "version", "online": True},
            {"id": "waiting", "phase": "pending", "online": False},
            {"id": "managed-b", "phase": "error", "online": True},
        ], "outdated": ["Managed A"],
    }})
    assert known["pending_invitations"] == 1
    assert known["blocked"] == 2
    assert known["follower_rows"]["managed-a"]["phase"] == "version"


@pytest.mark.parametrize("body,problem", [
    ({"ok": False}, "did not return data"),
    ({"ok": True, "data": {"self": {}, "leader": False}}, "self.id"),
    ({"ok": True, "data": {"self": {"id": "ks"}, "leader": None}}, "leader state"),
    ({"ok": True, "data": {"self": {"id": "ks"}, "leader": False,
                           "following": {"leader": None}}}, "malformed"),
    ({"ok": True, "data": {"self": {"id": "ks"}, "leader": False,
                           "following": {"leader": {}}}}, "omitted id"),
])
def test_malformed_status_cannot_change_confirmed_membership(body, problem):
    """[KSM-TEST-241] Partial command replies are rejected before placement."""
    with pytest.raises(ValueError, match=problem):
        fleet._status_from_response(body)
