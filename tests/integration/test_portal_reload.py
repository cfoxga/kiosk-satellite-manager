"""Reload each Portal's page once after HA starts (KSM-BEHAVE-183, #180).

The ESPHome entries, entity registry and state machine are real; only the
`button.press` service is recorded instead of reaching a kiosk."""
from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType

import pytest
from unittest.mock import AsyncMock, patch
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.kiosk_satellite_manager.const import CONF_ENTRY_TYPE, DOMAIN

from .test_global_settings import _manager


@pytest.fixture(autouse=True)
def offline_devices():
    """Keep the device health, fleetStatus and ADB permission polls off the
    network."""
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.1"})), patch(
        "custom_components.kiosk_satellite_manager.fleet.async_poll_device", new=AsyncMock()
    ), patch(
        "custom_components.kiosk_satellite_manager.permissions.async_refresh", new=AsyncMock()
    ):
        yield


def _parent(hass):
    parent = MockConfigEntry(domain=DOMAIN, title="Unmanaged", data={CONF_ENTRY_TYPE: "unmanaged"})
    parent.add_to_hass(hass)
    return parent


def _add_device(hass, parent, sub_id, host, profile):
    hass.config_entries.async_add_subentry(parent, ConfigSubentry(
        data=MappingProxyType({
            "host": host, "password": "synthetic-password", "tls_spki": None,
            "device_profile": profile,
        }), subentry_id=sub_id, subentry_type="device", title=sub_id, unique_id=sub_id,
    ))


def _reload_button(hass, host, mac, state):
    """An ESPHome entry at `host` with the KS Reload page button (prod's
    unique-ID shape) plus a Clear cache button registered first, so only the
    unique-ID match can pick Reload page."""
    entry = MockConfigEntry(domain="esphome", unique_id=mac, data={"host": host, "port": 6053, "device_name": mac})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    ids = {}
    for key, name in (("clear", "Clear cache"), ("reload", "Reload page")):
        ids[key] = registry.async_get_or_create(
            "button", "esphome", f"{mac.upper()}/0/button/{name}", config_entry=entry,
            suggested_object_id=f"{mac}_{key}",
        ).entity_id
        hass.states.async_set(ids[key], state)
    return ids


def _pressed(calls):
    return [call.data["entity_id"] for call in calls]


async def _boot(hass):
    """Set KSM up as HA bootstrap does: before HA is running. Returns the
    recorded `button.press` calls -- registered after setup, since KSM's
    button platform loads HA's own `button.press`."""
    hass.set_state(CoreState.not_running)
    await _manager(hass)
    return async_mock_service(hass, "button", "press")


async def _started(hass):
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()


async def test_portals_reload_once_after_start(hass):
    """[KSM-TEST-363] an available Portal button is pressed at start; an
    unavailable one when it comes back, once; a Google TV and an unlinked
    Portal never."""
    parent = _parent(hass)
    _add_device(hass, parent, "kitchen", "192.168.99.10", "portal_plus_gen2")
    _add_device(hass, parent, "mini", "192.168.99.11", "portal_mini")
    _add_device(hass, parent, "gtv", "192.168.99.12", "onn_4k_pro_android14")
    _add_device(hass, parent, "orphan", "192.168.99.13", "portal_gen2")
    kitchen = _reload_button(hass, "192.168.99.10", "aa", "unknown")
    mini = _reload_button(hass, "192.168.99.11", "bb", "unavailable")
    gtv = _reload_button(hass, "192.168.99.12", "cc", "unknown")

    calls = await _boot(hass)
    assert _pressed(calls) == []
    await _started(hass)
    assert _pressed(calls) == [kitchen["reload"]]

    hass.states.async_set(mini["reload"], "unknown")
    await hass.async_block_till_done()
    assert _pressed(calls) == [kitchen["reload"], mini["reload"]]

    hass.states.async_set(mini["reload"], "unavailable")
    hass.states.async_set(mini["reload"], "unknown")
    hass.states.async_set(gtv["reload"], "2026-10-03T00:00:00+00:00")
    await hass.async_block_till_done()
    assert _pressed(calls) == [kitchen["reload"], mini["reload"]]
    assert gtv["reload"] not in _pressed(calls)
    assert not {kitchen["clear"], mini["clear"], gtv["clear"]} & set(_pressed(calls))


async def test_setup_while_running_presses_nothing(hass):
    """[KSM-TEST-364] KSM loading after HA is already running (a reload or a
    fresh install) presses nothing, even when a button comes back later."""
    parent = _parent(hass)
    _add_device(hass, parent, "kitchen", "192.168.99.10", "portal_plus_gen2")
    _add_device(hass, parent, "mini", "192.168.99.11", "portal_mini")
    _reload_button(hass, "192.168.99.10", "aa", "unknown")
    mini = _reload_button(hass, "192.168.99.11", "bb", "unavailable")

    await _manager(hass)
    calls = async_mock_service(hass, "button", "press")
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    hass.states.async_set(mini["reload"], "unknown")
    await hass.async_block_till_done()
    assert _pressed(calls) == []


async def test_wait_ends_after_ten_minutes(hass):
    """[KSM-TEST-364] a Portal still unavailable after 10 minutes is not
    pressed when it finally comes back; one back at 9 minutes is."""
    parent = _parent(hass)
    _add_device(hass, parent, "late", "192.168.99.10", "portal_mini")
    _add_device(hass, parent, "slow", "192.168.99.11", "portal_mini")
    late = _reload_button(hass, "192.168.99.10", "aa", "unavailable")
    slow = _reload_button(hass, "192.168.99.11", "bb", "unavailable")

    calls = await _boot(hass)
    await _started(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=9))
    await hass.async_block_till_done()
    hass.states.async_set(slow["reload"], "unknown")
    await hass.async_block_till_done()
    assert _pressed(calls) == [slow["reload"]]

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    hass.states.async_set(late["reload"], "unknown")
    await hass.async_block_till_done()
    assert _pressed(calls) == [slow["reload"]]


async def test_failed_press_is_logged_and_others_still_reload(hass, caplog):
    """[KSM-TEST-364] one Portal's press raising does not stop the next
    Portal's press or fail KSM setup."""
    pressed: list[str] = []

    async def _press(call):
        entity_id = call.data["entity_id"]
        if entity_id.endswith("aa_reload"):
            raise HomeAssistantError("kiosk went away")
        pressed.append(entity_id)

    parent = _parent(hass)
    _add_device(hass, parent, "broken", "192.168.99.10", "portal_mini")
    _add_device(hass, parent, "fine", "192.168.99.11", "portal_mini")
    _reload_button(hass, "192.168.99.10", "aa", "unknown")
    fine = _reload_button(hass, "192.168.99.11", "bb", "unknown")

    await _boot(hass)
    hass.services.async_register("button", "press", _press)
    await _started(hass)
    assert pressed == [fine["reload"]]
    assert "kiosk went away" in caplog.text
    assert parent.state is ConfigEntryState.LOADED
