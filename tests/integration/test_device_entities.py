"""Per-device diagnostic entities (KSM-BEHAVE-079, issue #46): device type,
install recipe, IP address, and ADB enabled. Health and the ADB TCP probe are
mocked at their boundaries; the catalog lookups are the real source data.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.kiosk_satellite_manager.const import (
    CONF_DEVICE_PROFILE,
    CONF_ENTRY_TYPE,
    CONF_HOST,
    CONF_PORT,
    DOMAIN,
    ENTRY_TYPE_MANAGER,
)
from custom_components.kiosk_satellite_manager.device_catalog import require_recipe

from .conftest import init_integration

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_PROBE = "custom_components.kiosk_satellite_manager.binary_sensor.async_probe_adb_port"


def _state(hass, entry, suffix: str):
    entity = next(
        (e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
         if e.unique_id == f"{entry.entry_id}_{suffix}"),
        None,
    )
    assert entity is not None, f"no entity with unique_id suffix {suffix!r}"
    return hass.states.get(entity.entity_id)


async def test_device_type_recipe_and_ip_for_a_supported_model(hass):
    """[KSM-TEST-148] portal_go shows its catalog name, recipe, and health IP."""
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.79", "ip": "192.168.40.224"})):
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_go"})

    device_type = _state(hass, ctx.entry, "device_type")
    assert device_type.state == "Meta Portal Go"
    assert device_type.attributes["model_key"] == "portal_go"

    recipe = require_recipe("portal_go")
    recipe_state = _state(hass, ctx.entry, "recipe")
    assert recipe_state.state == recipe.recipe_key
    assert recipe_state.attributes["recipe_key"] == recipe.recipe_key
    assert "recipe_version" not in recipe_state.attributes
    assert recipe_state.attributes["recipe_name"] == recipe.name

    assert _state(hass, ctx.entry, "ip_address").state == "192.168.40.224"


async def test_unapproved_or_missing_model_never_names_a_recipe(hass):
    """[KSM-TEST-148] a fallback classification or no stored model gets recipe none."""
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.79"})):
        fallback = await init_integration(
            hass, data={CONF_HOST: "192.168.99.1", CONF_DEVICE_PROFILE: "gtv_stick"}
        )
        missing = await init_integration(hass, data={CONF_HOST: "192.168.99.2"})

    assert _state(hass, fallback.entry, "device_type").state == "gtv_stick"
    fallback_recipe = _state(hass, fallback.entry, "recipe")
    assert fallback_recipe.state == "none"
    assert fallback_recipe.attributes["reason"]
    assert "recipe_key" not in fallback_recipe.attributes

    assert _state(hass, missing.entry, "device_type").state == "unknown"
    assert _state(hass, missing.entry, "recipe").state == "none"


async def test_manager_entry_has_no_device_diagnostics(hass):
    """[KSM-TEST-148] negative case: diagnostics belong to device entries only."""
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.79"})):
        await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_go"})
    manager = next(
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER
    )
    unique_ids = {
        e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), manager.entry_id)
    }
    for suffix in ("device_type", "recipe", "ip_address", "adb_enabled"):
        assert f"{manager.entry_id}_{suffix}" not in unique_ids


async def test_ip_unavailable_when_health_unreachable_but_catalog_sensors_stay(hass):
    """[KSM-TEST-149] IP comes only from health; stored-data sensors stay up."""
    with patch(_HEALTH, new=AsyncMock(side_effect=aiohttp.ClientError("unreachable"))):
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_mini"})

    assert _state(hass, ctx.entry, "ip_address").state == "unavailable"
    assert _state(hass, ctx.entry, "device_type").state == "Meta Portal Mini"
    assert _state(hass, ctx.entry, "recipe").state not in ("unavailable", "none")


async def test_adb_enabled_follows_the_tcp_probe_independent_of_health(hass):
    """[KSM-TEST-150] on when the port connects, off when it does not."""
    probe = AsyncMock(return_value=True)
    with patch(_HEALTH, new=AsyncMock(side_effect=aiohttp.ClientError("unreachable"))), patch(
        _PROBE, new=probe
    ):
        ctx = await init_integration(
            hass, data={CONF_HOST: "portal.example.test", CONF_PORT: 5556}
        )
        state = _state(hass, ctx.entry, "adb_enabled")
        assert state.state == "on"
        assert probe.await_args.args[:2] == ("portal.example.test", 5556)

        probe.return_value = False
        assert await async_setup_component(hass, "homeassistant", {})
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": state.entity_id}, blocking=True
        )
        assert _state(hass, ctx.entry, "adb_enabled").state == "off"
