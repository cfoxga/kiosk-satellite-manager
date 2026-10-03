"""KSM-TEST-222/223 (KSM-BEHAVE-115, #73): Kiosk Satellite 2026.9.87+ has
native voice through ESPHome, so KSM no longer manages the separate Voice
Satellite integration -- no entry, no binding, no HACS repair, and no
`ha.satellite_entity` follower."""
from __future__ import annotations

import importlib.util
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.kiosk_satellite_manager.const import (
    CONF_AREA_ID, CONF_DEVICE_PROFILE, CONF_HOST, CONF_TLS_SPKI, DOMAIN,
)

from .conftest import init_integration

PIN = "ab" * 32
HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"


async def test_setup_leaves_voice_satellite_alone(hass):
    """[KSM-TEST-222] Device setup contacts no device, starts no Voice
    Satellite flow and raises no repair; the module is gone."""
    area = ar.async_get(hass).async_create("Great Room")
    with patch(HEALTH, new=AsyncMock(return_value={"name": "Great Room Kiosk"})), patch(
        "aiohttp.ClientSession._request", new=AsyncMock()
    ) as http:
        # A library model and an Area, so the device-support (KSM-BEHAVE-165)
        # and missing-Area (KSM-BEHAVE-178) repairs stay out of it.
        await init_integration(
            hass, data={CONF_HOST: "10.0.0.1", CONF_TLS_SPKI: PIN, CONF_DEVICE_PROFILE: "portal_go",
                        CONF_AREA_ID: area.id},
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    http.assert_not_awaited()
    assert [f for f in hass.config_entries.flow.async_progress() if f["handler"] == "voice_satellite"] == []
    assert [i for (d, i) in ir.async_get(hass).issues if d == DOMAIN] == []
    assert importlib.util.find_spec("custom_components.kiosk_satellite_manager.voice_satellite_link") is None


async def test_satellite_entity_rename_contacts_no_device(hass):
    """[KSM-TEST-223] Renaming an assist_satellite entity no longer logs in
    to any kiosk to re-point `ha.satellite_entity`."""
    with patch(HEALTH, new=AsyncMock(return_value={"name": "x"})):
        await init_integration(hass, data={CONF_HOST: "10.0.0.1", CONF_TLS_SPKI: PIN})
        await hass.async_block_till_done(wait_background_tasks=True)

    ent_reg = er.async_get(hass)
    old = ent_reg.async_get_or_create(
        "assist_satellite", "test", "great_room_kiosk", suggested_object_id="great_room_kiosk"
    ).entity_id
    with patch("aiohttp.ClientSession._request", new=AsyncMock()) as http, patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.login", new=AsyncMock()
    ) as login:
        ent_reg.async_update_entity(old, new_entity_id="assist_satellite.master_bedroom_kiosk")
        await hass.async_block_till_done(wait_background_tasks=True)

    http.assert_not_awaited()
    login.assert_not_awaited()
