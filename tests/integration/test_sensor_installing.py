"""Version sensor's transitional "Installing" state (KSM-BEHAVE-007).
Confirmed live that with no distinct state, the sensor just reads
"unavailable" for the whole install+boot window, which reads as broken
rather than in-progress."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.helpers import entity_registry as er

from custom_components.kiosk_satellite_manager.const import DOMAIN

from .conftest import init_integration


async def test_sensor_shows_installing_while_coordinator_flagged(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "1.0.0"}),
    ):
        ctx = await init_integration(hass)

    coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
    ent_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
    sensor_entry = next(e for e in entries if e.domain == "sensor")

    coordinator.ksm_installing = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(sensor_entry.entity_id)
    assert state.state == "Installing"

    coordinator.ksm_installing = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(sensor_entry.entity_id).state == "1.0.0"


async def test_sensor_available_while_installing_even_if_health_never_succeeded(hass):
    import aiohttp

    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(side_effect=aiohttp.ClientConnectionError("unreachable")),
    ):
        ctx = await init_integration(hass)

    coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
    ent_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
    sensor_entry = next(e for e in entries if e.domain == "sensor")
    assert hass.states.get(sensor_entry.entity_id).state == "unavailable"

    coordinator.ksm_installing = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(sensor_entry.entity_id).state == "Installing"
