"""Real config-entry setup smoke test -- exercises __init__.py's actual
async_setup_entry against a live (test) hass via phacc, not a mock."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.helpers import entity_registry as er

from custom_components.kiosk_satellite_manager.const import DOMAIN

from .conftest import init_integration


async def test_setup_entry_starts_coordinator_and_platforms(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.61", "name": "Test Device"}),
    ):
        ctx = await init_integration(hass)

    assert DOMAIN in hass.data
    coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
    assert coordinator.data == {"appVersion": "2026.9.61", "name": "Test Device"}

    ent_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
    domains = {e.domain for e in entries}
    assert domains == {"sensor", "button"}

    sensor_entry = next(e for e in entries if e.domain == "sensor")
    sensor_state = hass.states.get(sensor_entry.entity_id)
    assert sensor_state is not None
    assert sensor_state.state == "2026.9.61"


async def test_unload_entry_removes_coordinator(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "1.0.0"}),
    ):
        ctx = await init_integration(hass)
    assert await hass.config_entries.async_unload(ctx.entry.entry_id)
    await hass.async_block_till_done()
    assert ctx.entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_setup_entry_succeeds_when_device_unprovisioned(hass):
    """KSM-BEHAVE-006: a fresh device with no Kiosk Satellite app installed yet
    (health endpoint unreachable) must still complete setup so the Install
    button becomes available -- setup must not depend on the device already
    being provisioned."""
    import aiohttp

    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(
            side_effect=aiohttp.ClientConnectionError(
                "Cannot connect to host test-portal.cfoxga.com:2324 ssl:default "
                "[Connect call failed ('192.168.40.224', 2324)]"
            )
        ),
    ):
        ctx = await init_integration(hass)

    assert DOMAIN in hass.data
    coordinator = hass.data[DOMAIN][ctx.entry.entry_id]
    assert coordinator.last_update_success is False

    ent_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, ctx.entry.entry_id)
    domains = {e.domain for e in entries}
    assert domains == {"sensor", "button"}

    button_entry = next(e for e in entries if e.domain == "button")
    button_state = hass.states.get(button_entry.entity_id)
    assert button_state is not None
    assert button_state.state != "unavailable"
