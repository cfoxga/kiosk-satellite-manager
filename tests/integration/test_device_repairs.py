"""Per-device repairs end with the device (KSM-BEHAVE-154, #126)."""
from __future__ import annotations

from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import fleet
from custom_components.kiosk_satellite_manager.const import CONF_ENTRY_TYPE, DOMAIN

from .conftest import init_integration
from .test_global_settings import _manager

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_REVOKE_SUBENTRY = "custom_components.kiosk_satellite_manager.credentials.async_revoke_owned_credential"
_REVOKE_ENTRY = "custom_components.kiosk_satellite_manager.async_revoke_owned_credential"


def _raise_repairs(hass, device_id: str) -> None:
    for issue_id in (f"tls_certificate_changed_{device_id}", f"dashboard_dns_{device_id}",
                     f"area_required_{device_id}"):
        ir.async_create_issue(
            hass, DOMAIN, issue_id, is_fixable=False,
            severity=ir.IssueSeverity.WARNING, translation_key="dashboard_dns_mismatch",
        )


def _repairs(hass, device_id: str) -> set[str]:
    registry = ir.async_get(hass)
    return {
        issue_id
        for issue_id in (f"tls_certificate_changed_{device_id}", f"dashboard_dns_{device_id}",
                         f"area_required_{device_id}")
        if registry.async_get_issue(DOMAIN, issue_id) is not None
    }


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


async def test_KSM_TEST_310_removing_a_device_entry_clears_its_repairs(hass):
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        ctx = await init_integration(hass)
    _raise_repairs(hass, ctx.entry.entry_id)
    _raise_repairs(hass, "another-device")

    with patch(_REVOKE_ENTRY, new=AsyncMock()):
        await hass.config_entries.async_remove(ctx.entry.entry_id)
        await hass.async_block_till_done()

    assert _repairs(hass, ctx.entry.entry_id) == set()
    assert len(_repairs(hass, "another-device")) == 3


async def test_KSM_TEST_310_deleting_a_device_subentry_clears_its_repairs(hass):
    unmanaged = await _unmanaged_with(hass, _device("dev-a", 31), _device("dev-b", 32))
    _raise_repairs(hass, "dev-a")
    _raise_repairs(hass, "dev-b")

    with patch(_REVOKE_SUBENTRY, new=AsyncMock()), patch(
        _HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})
    ):
        hass.config_entries.async_remove_subentry(unmanaged, "dev-a")
        await hass.async_block_till_done()

    assert _repairs(hass, "dev-a") == set()
    assert len(_repairs(hass, "dev-b")) == 3


async def test_KSM_TEST_310_a_fleet_move_keeps_the_device_repairs(hass):
    unmanaged = await _unmanaged_with(hass, _device("dev-a", 31))
    target = MockConfigEntry(
        domain=DOMAIN, title="Fleet - Leader", unique_id="ksm_fleet:leader",
        data={CONF_ENTRY_TYPE: "fleet", "leader_id": "leader"},
    )
    target.add_to_hass(hass)
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})):
        assert await hass.config_entries.async_setup(target.entry_id)
        await hass.async_block_till_done()
    _raise_repairs(hass, "dev-a")

    with patch(_REVOKE_SUBENTRY, new=AsyncMock()) as revoke, patch(
        _HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})
    ):
        await fleet._move_device(hass, fleet.resolve_device(hass, "dev-a"), target)
        await hass.async_block_till_done()

    assert "dev-a" in target.subentries and "dev-a" not in unmanaged.subentries
    revoke.assert_not_awaited()
    assert len(_repairs(hass, "dev-a")) == 3


async def test_KSM_TEST_310_a_legacy_migration_keeps_the_device_repairs(hass):
    await _manager(hass)
    unmanaged = fleet.unmanaged_entry(hass)
    old = MockConfigEntry(domain=DOMAIN, title="Kitchen", data={
        "host": "192.168.99.121", "port": 5555, "key_path": "/tmp/adbkey",
        "password": "synthetic-secret", "name": "Kitchen",
    })
    old.add_to_hass(hass)
    _raise_repairs(hass, old.entry_id)

    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.87"})), patch(
        _REVOKE_ENTRY, new=AsyncMock()
    ) as revoke, patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.login",
        new=AsyncMock(return_value="test-token"),
    ), patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.run_command",
        new=AsyncMock(return_value={"ok": True, "data": {
            "self": {"id": "kitchen-ks"}, "leader": False, "following": None,
        }}),
    ):
        # conftest stubs the public wrapper outside test_fleet_entries.py.
        await fleet._async_migrate_legacy_device(hass, old, unmanaged)
        await hass.async_block_till_done()

    assert hass.config_entries.async_get_entry(old.entry_id) is None
    assert old.entry_id in unmanaged.subentries
    revoke.assert_not_awaited()
    assert len(_repairs(hass, old.entry_id)) == 3


async def test_KSM_TEST_310_removing_a_grouping_entry_clears_and_revokes_its_devices(hass):
    unmanaged = await _unmanaged_with(
        hass,
        _device("dev-a", 31, ha_token="tok-a", ha_token_owned=True, ha_refresh_token_id="rt-a"),
        _device("dev-b", 32, ha_token="tok-b", ha_token_owned=True, ha_refresh_token_id="rt-b"),
    )
    _raise_repairs(hass, "dev-a")
    _raise_repairs(hass, "dev-b")
    _raise_repairs(hass, "another-device")

    with patch(_REVOKE_ENTRY, new=AsyncMock()) as revoke:
        await hass.config_entries.async_remove(unmanaged.entry_id)
        await hass.async_block_till_done()

    assert _repairs(hass, "dev-a") == set() and _repairs(hass, "dev-b") == set()
    assert len(_repairs(hass, "another-device")) == 3
    revoked = {call.args[1].refresh_token_id for call in revoke.await_args_list
               if call.args[1] is not None}
    assert revoked == {"rt-a", "rt-b"}


async def test_fix_flows_abort_when_the_device_is_gone(hass):
    """KSM-BEHAVE-154: every per-device fix flow aborts once its device is removed."""
    from custom_components.kiosk_satellite_manager import repairs

    for flow in (
        repairs.AreaRequiredFlow("gone"),
        repairs.TlsCertificateChangedFlow("gone"),
        repairs.TlsDisabledFlow("gone"),
        repairs.DeviceSupportFlow("gone"),
    ):
        flow.hass = hass
        result = await flow.async_step_init()
        assert result["type"] == "abort", type(flow).__name__
        assert result["reason"] == "entry_not_found", type(flow).__name__
