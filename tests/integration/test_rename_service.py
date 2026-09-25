"""Rename device service integration tests (KSM-BEHAVE-084/085, #48,
KSM-TEST-161-164)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import SERVICE_RENAME_DEVICE
from custom_components.kiosk_satellite_manager.const import CONF_HOST, CONF_NAME, DOMAIN
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

from .conftest import admin_context, init_integration

_ADB_CLIENT = "custom_components.kiosk_satellite_manager.AdbClient"
_LOGIN = "custom_components.kiosk_satellite_manager.ks_api_login"
_APPLY_KS = "custom_components.kiosk_satellite_manager.apply_rename_ks_settings"
_RESOLVE_DNS = "custom_components.kiosk_satellite_manager.resolve_and_verify_dns_host"
_FETCH_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_RESOLVE_HOST = "custom_components.kiosk_satellite_manager.rename._resolve_host"
_TIMEOUT = "custom_components.kiosk_satellite_manager.rename.ESPHOME_RENAME_TIMEOUT"
_INTERVAL = "custom_components.kiosk_satellite_manager.rename._WAIT_INTERVAL"


async def _call_rename(hass, entry_id: str, name: str, context: Context):
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_RENAME_DEVICE,
        {"config_entry_id": entry_id, "name": name},
        blocking=True,
        context=context,
        return_response=True,
    )


async def test_rename_device_full_success_updates_entry_and_host(hass):
    """[KSM-TEST-163/164] verified rename updates entry title/name and,
    once the DNS candidate resolves and verifies, CONF_HOST."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: "old-device.devices.example.com"})

        with patch(_ADB_CLIENT) as mock_client_cls, patch(
            _LOGIN, new=AsyncMock(return_value="device-token")
        ), patch(_APPLY_KS, new=AsyncMock(return_value="applied")), patch(
            _RESOLVE_DNS, new=AsyncMock(return_value=True)
        ):
            result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    mock_client_cls.assert_not_called()
    assert result == {
        "ks": "applied",
        "android": "unsupported",
        "entry": "applied",
        "host": "applied",
        "esphome": "not_found",
    }
    assert ctx.entry.title == "Great Room Device"
    assert ctx.entry.data[CONF_NAME] == "Great Room Device"
    assert ctx.entry.data[CONF_HOST] == "great-room-device.devices.example.com"


async def test_rename_device_repeated_call_is_idempotent(hass):
    """[KSM-TEST-164] a second call with the same already-applied name
    reports unchanged/unchanged rather than re-mutating."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(
            hass,
            data={
                CONF_HOST: "great-room-device.devices.example.com",
                CONF_NAME: "Great Room Device",
            },
        )
        hass.config_entries.async_update_entry(ctx.entry, title="Great Room Device")

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
            _APPLY_KS, new=AsyncMock(return_value="unchanged")
        ):
            result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["ks"] == "unchanged"
    assert result["entry"] == "unchanged"
    assert result["host"] == "unchanged"


async def test_rename_device_unresolved_dns_candidate_reports_pending_and_keeps_host(hass):
    """[KSM-TEST-163] DNS candidate that doesn't resolve to the same device
    leaves CONF_HOST unchanged and reports host: pending."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: "old-device.devices.example.com"})

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
            _APPLY_KS, new=AsyncMock(return_value="applied")
        ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)):
            result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["host"] == "pending"
    assert ctx.entry.data[CONF_HOST] == "old-device.devices.example.com"


async def test_rename_device_ip_host_is_unchanged_not_pending(hass):
    """[KSM-TEST-163] an IP-address host has no DNS label to migrate."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: "192.168.99.99"})

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
            _APPLY_KS, new=AsyncMock(return_value="applied")
        ):
            result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["host"] == "unchanged"
    assert ctx.entry.data[CONF_HOST] == "192.168.99.99"


async def test_rename_device_ks_failure_returns_partial_result_not_raise(hass):
    """[KSM-TEST-162] a KS-layer failure is a returned result, not a raise --
    the caller (HAM) needs the structured partial result to react."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass)

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")), patch(
            _APPLY_KS, new=AsyncMock(side_effect=KsApiError("device rejected settings: ['device.name']"))
        ):
            result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["ks"] == "failed"
    assert "error" in result
    assert result["esphome"] == "unchanged"
    assert ctx.entry.data.get(CONF_NAME) != "Great Room Device"


async def test_rename_device_rejects_blank_slug_name(hass):
    """[KSM-TEST-162] negative: a name producing an empty slug is rejected
    before any device I/O."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass)

        with patch(_ADB_CLIENT) as mock_client_cls, patch(_LOGIN) as mock_login:
            with pytest.raises(ServiceValidationError):
                await _call_rename(hass, ctx.entry.entry_id, "!!!", await admin_context(hass))
    mock_client_cls.assert_not_called()
    mock_login.assert_not_called()


async def test_rename_device_rejects_unknown_config_entry(hass):
    """[KSM-TEST-161] negative: an unknown target causes zero device I/O."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "irrelevant"})):
        await init_integration(hass)

        with patch(_LOGIN) as mock_login:
            with pytest.raises(ServiceValidationError):
                await _call_rename(hass, "does-not-exist", "Great Room Device", await admin_context(hass))
    mock_login.assert_not_called()


# --- ESPHome action re-registration after a node rename (KSM-BEHAVE-092, #56) ---

_KSM_HOST = "old-device.devices.example.com"
_KIOSK_IP = "192.168.40.59"
_DNS = {_KSM_HOST: _KIOSK_IP, "other-kiosk.devices.example.com": "192.168.40.60"}


async def _fake_resolve(hass, host):
    return _DNS.get(host)


def _esphome_entry(hass, *, host=_KIOSK_IP, device_name="old-device", mac="42:26:72:7e:2a:e1"):
    entry = MockConfigEntry(
        domain="esphome", unique_id=mac, data={"host": host, "port": 6053, "device_name": device_name}
    )
    entry.add_to_hass(hass)
    return entry


def _register_esphome_actions(hass, prefix, suffixes=("notification", "launch_app")):
    for suffix in suffixes:
        hass.services.async_register("esphome", f"{prefix}_{suffix}", lambda call: None)


def _ks_rename_side_effect(hass, esphome_entry, new_node="great-room-device"):
    """KS restarts its ESPHome server on a node change; HA's ESPHome manager
    then stores the new device_name on reconnect (manager.py `_on_connect`).
    The reconnect lands a moment after the PATCH returns, so a reload that
    does not wait for it sees the old name."""

    def _record_new_name():
        hass.config_entries.async_update_entry(
            esphome_entry, data={**esphome_entry.data, "device_name": new_node}
        )

    async def _apply(*_args, **_kwargs):
        hass.loop.call_later(0.05, _record_new_name)
        return "applied"

    return _apply


async def _setup_callers(hass):
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "leak_alert",
                    "alias": "Leak alert",
                    "triggers": [{"trigger": "event", "event_type": "ksm_test_leak"}],
                    "actions": [
                        {"action": "esphome.old_device_notification", "data": {"message": "Leak"}}
                    ],
                },
                {
                    "id": "unrelated",
                    "alias": "Unrelated",
                    "triggers": [{"trigger": "event", "event_type": "ksm_test_other"}],
                    "actions": [{"action": "esphome.other_kiosk_notification", "data": {}}],
                },
            ]
        },
    )
    assert await async_setup_component(
        hass,
        "script",
        {"script": {"open_clock": {"sequence": [{"action": "esphome.old_device_launch_app"}]}}},
    )
    await hass.async_block_till_done()


async def test_rename_device_reloads_linked_esphome_entry_and_reports_callers(hass):
    """[KSM-TEST-173] the node rename reloads exactly the ESPHome entry whose
    host resolves to the KSM host's IP -- only after HA has recorded the new
    device_name -- and reports applied once every prior action is registered
    under the new prefix, with the old-prefix callers listed."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: _KSM_HOST})
    await _setup_callers(hass)
    linked = _esphome_entry(hass)
    other = _esphome_entry(hass, host="other-kiosk.devices.example.com", device_name="other-kiosk", mac="aa:bb:cc:dd:ee:ff")
    _register_esphome_actions(hass, "old_device")
    reloaded: list[tuple[str, str]] = []

    async def _reload(entry_id):
        entry = hass.config_entries.async_get_entry(entry_id)
        reloaded.append((entry_id, entry.data["device_name"]))
        _register_esphome_actions(hass, "great_room_device")
        return True

    with patch(_RESOLVE_HOST, new=_fake_resolve), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(
        _APPLY_KS, new=AsyncMock(side_effect=_ks_rename_side_effect(hass, linked))
    ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)), patch(
        _TIMEOUT, 1.0
    ), patch(_INTERVAL, 0.01), patch.object(hass.config_entries, "async_reload", new=_reload):
        result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert reloaded == [(linked.entry_id, "great-room-device")]
    assert other.entry_id not in [r[0] for r in reloaded]
    assert result["esphome"] == "applied"
    assert result["esphome_actions"] == {
        "old_prefix": "old_device",
        "new_prefix": "great_room_device",
        "callers": ["automation.leak_alert", "script.open_clock"],
    }


@pytest.mark.parametrize("duplicate", [False, True])
async def test_rename_device_without_single_esphome_match_reloads_nothing(hass, duplicate):
    """[KSM-TEST-173] negative: no ESPHome entry on the kiosk's IP, or two
    of them, is not_found -- KSM never guesses which entry to reload."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: _KSM_HOST})
    _esphome_entry(hass, host="other-kiosk.devices.example.com", device_name="other-kiosk", mac="aa:bb:cc:dd:ee:ff")
    if duplicate:
        _esphome_entry(hass)
        _esphome_entry(hass, host=_KSM_HOST, device_name="old-device-2", mac="11:22:33:44:55:66")
    reload = AsyncMock(return_value=True)

    with patch(_RESOLVE_HOST, new=_fake_resolve), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(
        _APPLY_KS, new=AsyncMock(return_value="applied")
    ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)), patch(
        _TIMEOUT, 0.05
    ), patch(_INTERVAL, 0.01), patch.object(hass.config_entries, "async_reload", new=reload):
        result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    reload.assert_not_called()
    assert result["esphome"] == "not_found"
    assert "esphome_actions" not in result


async def test_rename_device_esphome_actions_missing_after_reload_is_pending(hass):
    """[KSM-TEST-174] negative: a reload after which the old actions never
    appear under the new prefix reports pending, not applied."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: _KSM_HOST})
    linked = _esphome_entry(hass)
    _register_esphome_actions(hass, "old_device")
    # A longer sibling prefix must not be mistaken for this kiosk's actions.
    _esphome_entry(hass, host="other-kiosk.devices.example.com", device_name="old-device-annex", mac="aa:bb:cc:dd:ee:ff")
    _register_esphome_actions(hass, "old_device_annex", ("notification",))
    reload = AsyncMock(return_value=True)

    async def _partial_reload(entry_id):
        await reload(entry_id)
        _register_esphome_actions(hass, "great_room_device", ("notification",))
        return True

    with patch(_RESOLVE_HOST, new=_fake_resolve), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(
        _APPLY_KS, new=AsyncMock(side_effect=_ks_rename_side_effect(hass, linked))
    ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)), patch(
        _TIMEOUT, 0.3
    ), patch(_INTERVAL, 0.01), patch.object(hass.config_entries, "async_reload", new=_partial_reload):
        result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    reload.assert_awaited_once_with(linked.entry_id)
    assert result["esphome"] == "pending"
    assert result["esphome_actions"]["callers"] == []


async def test_rename_device_esphome_actions_complete_despite_sibling_prefix(hass):
    """[KSM-TEST-174] positive control for the sibling-prefix case above:
    once this kiosk's own two actions re-register, the sibling's
    `old_device_annex_notification` is not demanded under the new prefix."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: _KSM_HOST})
    linked = _esphome_entry(hass)
    _register_esphome_actions(hass, "old_device")
    _esphome_entry(hass, host="other-kiosk.devices.example.com", device_name="old-device-annex", mac="aa:bb:cc:dd:ee:ff")
    _register_esphome_actions(hass, "old_device_annex", ("notification",))

    async def _reload(entry_id):
        _register_esphome_actions(hass, "great_room_device")
        return True

    with patch(_RESOLVE_HOST, new=_fake_resolve), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(
        _APPLY_KS, new=AsyncMock(side_effect=_ks_rename_side_effect(hass, linked))
    ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)), patch(
        _TIMEOUT, 0.5
    ), patch(_INTERVAL, 0.01), patch.object(hass.config_entries, "async_reload", new=_reload):
        result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["esphome"] == "applied"


async def test_rename_device_esphome_reload_error_is_failed(hass):
    """[KSM-TEST-174] negative: a reload that raises reports failed and the
    rest of the result still returns."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: _KSM_HOST})
    linked = _esphome_entry(hass)
    _register_esphome_actions(hass, "old_device")

    with patch(_RESOLVE_HOST, new=_fake_resolve), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(
        _APPLY_KS, new=AsyncMock(side_effect=_ks_rename_side_effect(hass, linked))
    ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)), patch(
        _TIMEOUT, 0.5
    ), patch(_INTERVAL, 0.01), patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(side_effect=HomeAssistantError("boom"))
    ):
        result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["esphome"] == "failed"
    assert result["entry"] == "applied"


async def test_rename_device_esphome_already_renamed_is_unchanged_without_reload(hass):
    """[KSM-TEST-174] a repeated call whose node already matches, with the
    actions registered under the new prefix, does not reload again."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: _KSM_HOST, CONF_NAME: "Great Room Device"})
    hass.config_entries.async_update_entry(ctx.entry, title="Great Room Device")
    _esphome_entry(hass, device_name="great-room-device")
    _register_esphome_actions(hass, "great_room_device")
    reload = AsyncMock(return_value=True)

    with patch(_RESOLVE_HOST, new=_fake_resolve), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(
        _APPLY_KS, new=AsyncMock(return_value="unchanged")
    ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)), patch(
        _TIMEOUT, 0.05
    ), patch(_INTERVAL, 0.01), patch.object(hass.config_entries, "async_reload", new=reload):
        result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    reload.assert_not_called()
    assert result["esphome"] == "unchanged"
    assert "esphome_actions" not in result


async def test_rename_device_ks_failure_never_reloads_esphome(hass):
    """[KSM-TEST-174] negative: a failed KS layer leaves the ESPHome entry
    untouched."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: _KSM_HOST})
    _esphome_entry(hass)
    _register_esphome_actions(hass, "old_device")
    reload = AsyncMock(return_value=True)

    with patch(_RESOLVE_HOST, new=_fake_resolve), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(
        _APPLY_KS, new=AsyncMock(side_effect=KsApiError("rejected"))
    ), patch.object(hass.config_entries, "async_reload", new=reload):
        result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    reload.assert_not_called()
    assert result["ks"] == "failed"
    assert result["esphome"] == "unchanged"


async def test_rename_device_unresolvable_ksm_host_is_not_found(hass):
    """[KSM-TEST-173] negative: a KSM host that does not resolve cannot be
    matched to any ESPHome entry, so nothing reloads."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: "gone.devices.example.com"})
    _esphome_entry(hass)
    reload = AsyncMock(return_value=True)

    with patch(_RESOLVE_HOST, new=_fake_resolve), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(
        _APPLY_KS, new=AsyncMock(return_value="applied")
    ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)), patch.object(
        hass.config_entries, "async_reload", new=reload
    ):
        result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    reload.assert_not_called()
    assert result["esphome"] == "not_found"


async def test_rename_device_node_matches_without_actions_still_reloads(hass):
    """[KSM-TEST-174] a node that already matches but has no action under the
    new prefix (a retry after HA recorded the name, or a proxy-only kiosk)
    reloads rather than claiming unchanged, and reports no action prefixes."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: _KSM_HOST})
    linked = _esphome_entry(hass, device_name="great-room-device")
    reload = AsyncMock(return_value=True)

    with patch(_RESOLVE_HOST, new=_fake_resolve), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(
        _APPLY_KS, new=AsyncMock(return_value="unchanged")
    ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)), patch(
        _TIMEOUT, 0.05
    ), patch(_INTERVAL, 0.01), patch.object(hass.config_entries, "async_reload", new=reload):
        result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    reload.assert_awaited_once_with(linked.entry_id)
    assert result["esphome"] == "applied"
    assert "esphome_actions" not in result


async def test_rename_device_esphome_reload_returning_false_is_failed(hass):
    """[KSM-TEST-174] negative: a reload HA reports as unsuccessful is failed,
    never applied."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: _KSM_HOST})
    linked = _esphome_entry(hass)
    _register_esphome_actions(hass, "old_device")

    with patch(_RESOLVE_HOST, new=_fake_resolve), patch(_LOGIN, new=AsyncMock(return_value="t")), patch(
        _APPLY_KS, new=AsyncMock(side_effect=_ks_rename_side_effect(hass, linked))
    ), patch(_RESOLVE_DNS, new=AsyncMock(return_value=False)), patch(
        _TIMEOUT, 0.5
    ), patch(_INTERVAL, 0.01), patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=False)
    ):
        result = await _call_rename(hass, ctx.entry.entry_id, "Great Room Device", await admin_context(hass))

    assert result["esphome"] == "failed"
    assert result["esphome_actions"]["old_prefix"] == "old_device"
