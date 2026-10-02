"""KSM-TEST-212/213 (#67): every managed kiosk gets an ESPHome node name
derived from its name; ESPHome itself is turned on only for a device added
while the manager's "ESPHome on new devices" option is on, and only once."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow

from custom_components.kiosk_satellite_manager import esphome_identity
from custom_components.kiosk_satellite_manager.const import (
    CONF_ESPHOME_ENABLE_PENDING,
    CONF_ESPHOME_NEW_DEVICES,
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    DOMAIN,
)
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

from .conftest import init_integration

KS = "custom_components.kiosk_satellite_manager.ks_api_client"
HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
FLOW = "custom_components.kiosk_satellite_manager.config_flow"


@pytest.fixture
def ks_api():
    settings = {"esphome.node_name": "", "esphome.enabled": False, "esphome.entities": False}
    get_settings = AsyncMock(side_effect=lambda *a, **k: dict(settings))
    patch_settings = AsyncMock(return_value={})
    with patch(f"{KS}.login", new=AsyncMock(return_value="ks-token")), patch(
        f"{KS}.get_settings", new=get_settings
    ), patch(f"{KS}.patch_settings", new=patch_settings), patch(
        HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.88"})
    ), patch(
        "custom_components.kiosk_satellite_manager.async_ensure_esphome_identity",
        new=esphome_identity.async_ensure_esphome_identity,
    ):
        yield settings, patch_settings


def _esphome_patches(patch_settings) -> list[dict]:
    return [
        {k: v for k, v in call.args[3].items() if k.startswith("esphome.")}
        for call in patch_settings.await_args_list
        if any(k.startswith("esphome.") for k in call.args[3])
    ]


async def _add_device(hass, name="Great Room Kiosk", **data):
    ctx = await init_integration(hass, data={CONF_NAME: name, **data})
    await hass.async_block_till_done(wait_background_tasks=True)
    return ctx.entry


async def test_empty_node_name_is_set_from_the_device_name(hass, ks_api):
    """[KSM-TEST-212] An empty node name gets the KSM-BEHAVE-084 slug of the
    device's name; ESPHome stays off."""
    _, patch_settings = ks_api
    await _add_device(hass)
    assert _esphome_patches(patch_settings) == [{"esphome.node_name": "great-room-kiosk"}]


@pytest.mark.parametrize("generated", ["kiosk-satellite-838f3d", "kiosk-satellite-00ab12"])
async def test_ks_generated_node_name_is_replaced(hass, ks_api, generated):
    """[KSM-TEST-212] Kiosk Satellite fills a blank node name with its own
    `kiosk-satellite-<6 hex>` once ESPHome runs (seen live on dev); that is
    not a name anyone chose, so KSM replaces it."""
    settings, patch_settings = ks_api
    settings["esphome.node_name"] = generated
    await _add_device(hass)
    assert _esphome_patches(patch_settings) == [{"esphome.node_name": "great-room-kiosk"}]


@pytest.mark.parametrize("chosen", ["custom-node", "kiosk-satellite-den", "kiosk-satellite-838f3d0"])
async def test_existing_node_name_and_disabled_esphome_are_left_alone(hass, ks_api, chosen):
    """[KSM-TEST-212] Negative: a node name the device already has is never
    overwritten, and without the pending flag a disabled ESPHome is never
    turned on."""
    settings, patch_settings = ks_api
    settings["esphome.node_name"] = chosen
    await _add_device(hass)
    assert _esphome_patches(patch_settings) == []


async def test_node_name_failures_never_fail_the_device(hass, ks_api):
    """[KSM-TEST-212] Negative: a KS rejection or a missing password leaves
    the entry loaded."""
    _, patch_settings = ks_api
    patch_settings.side_effect = KsApiError("rejected")
    entry = await _add_device(hass)
    assert entry.state is config_entries.ConfigEntryState.LOADED

    patch_settings.reset_mock()
    patch_settings.side_effect = None
    entry = await _add_device(hass, name="Den Kiosk", host="192.168.99.98", password="")
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert _esphome_patches(patch_settings) == []


async def test_pending_flag_turns_esphome_on_once(hass, ks_api):
    """[KSM-TEST-213] A device added with the option on gets ESPHome turned
    on with its node name, the flag is cleared, and a reload does not turn it
    on again (the operator may have turned it off)."""
    settings, patch_settings = ks_api
    entry = await _add_device(hass, **{CONF_ESPHOME_ENABLE_PENDING: True})
    assert _esphome_patches(patch_settings) == [
        {"esphome.node_name": "great-room-kiosk", "esphome.enabled": True, "esphome.entities": True}
    ]
    assert CONF_ESPHOME_ENABLE_PENDING not in entry.data

    settings["esphome.node_name"] = "great-room-kiosk"
    patch_settings.reset_mock()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _esphome_patches(patch_settings) == []


async def test_pending_flag_turns_entities_on_when_only_the_server_runs(hass, ks_api):
    """[KSM-TEST-298] The pending flag on a device whose ESPHome server already
    runs with entities off turns entities on, alone."""
    settings, patch_settings = ks_api
    settings.update({"esphome.node_name": "great-room-kiosk", "esphome.enabled": True})
    await _add_device(hass, **{CONF_ESPHOME_ENABLE_PENDING: True})
    assert _esphome_patches(patch_settings) == [{"esphome.entities": True}]


async def test_pending_flag_survives_a_failed_attempt(hass, ks_api):
    """[KSM-TEST-213] Negative: when the PATCH fails the flag stays, so the
    next setup tries again."""
    _, patch_settings = ks_api
    patch_settings.side_effect = KsApiError("offline")
    entry = await _add_device(hass, **{CONF_ESPHOME_ENABLE_PENDING: True})
    assert entry.data[CONF_ESPHOME_ENABLE_PENDING] is True


@pytest.mark.parametrize("option", [True, False, None])
async def test_adoption_copies_the_manager_option(hass, ks_health_probe, tls_migration, option):
    """[KSM-TEST-213] The manager's option (default off) is copied into the
    adopted device's entry as the pending flag."""
    ks_health_probe.return_value = (None, {"appVersion": "2026.9.88", "name": "Great Room Kiosk"})
    tls_migration.return_value = None
    manager = {} if option is None else {CONF_ESPHOME_NEW_DEVICES: option}
    with patch(f"{FLOW}._manager_options", return_value=manager), patch(
        f"{FLOW}.login", new=AsyncMock(return_value="tok")
    ), patch(HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.88"})), patch(
        f"{FLOW}.esphome_adopt.async_adopt", new=AsyncMock(return_value="added")
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.40.45", "port": 5555}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "hunter222"}
        )
        await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ESPHOME_ENABLE_PENDING] is bool(option)
