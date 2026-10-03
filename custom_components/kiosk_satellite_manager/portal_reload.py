"""Reload each Portal's page once after Home Assistant starts (KSM-BEHAVE-183, #180).

After an HA restart a Portal's dashboard can sit on stale data until its page
is reloaded. KSM presses the Kiosk Satellite **Reload page** button (exposed
through the device's own ESPHome entry) once per Portal: at once when the
button is already available, otherwise the first time it becomes available,
waiting at most RELOAD_WAIT. Only Meta Portals; never a TV, where a reload
could interrupt what is playing. Never on an integration reload.
"""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE
from homeassistant.core import CoreState, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import CONF_DEVICE_PROFILE
from . import fleet
from .rename import find_esphome_link

_LOGGER = logging.getLogger(__name__)

PORTAL_PROFILE_PREFIX = "portal_"
RELOAD_PAGE_UNIQUE_ID_SUFFIX = "/button/Reload page"
RELOAD_WAIT = timedelta(minutes=10)


def _is_portal(device: fleet.DeviceEntry) -> bool:
    return str(device.data.get(CONF_DEVICE_PROFILE) or "").startswith(PORTAL_PROFILE_PREFIX)


def _available(state: State | None) -> bool:
    return state is not None and state.state != STATE_UNAVAILABLE


async def _reload_button(hass: HomeAssistant, device: fleet.DeviceEntry) -> str | None:
    """The enabled Reload page button of the device's linked ESPHome entry."""
    link = await find_esphome_link(hass, device.data[CONF_HOST])
    if link is None:
        return None
    for item in er.async_entries_for_config_entry(er.async_get(hass), link.entry.entry_id):
        if (item.domain == "button" and item.disabled_by is None
                and item.unique_id.endswith(RELOAD_PAGE_UNIQUE_ID_SUFFIX)):
            return item.entity_id
    return None


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    try:
        await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    except HomeAssistantError as err:
        _LOGGER.warning("Startup page reload failed for %s: %s", entity_id, err)
    else:
        _LOGGER.info("Startup page reload sent to %s", entity_id)


@callback
def _press_when_available(hass: HomeAssistant, entity_id: str) -> None:
    unsubs: list = []

    @callback
    def _stop() -> None:
        while unsubs:
            unsubs.pop()()

    @callback
    def _changed(event: Event) -> None:
        if _available(event.data.get("new_state")):
            _stop()
            hass.async_create_task(_press(hass, entity_id))

    @callback
    def _give_up(_now) -> None:
        _stop()
        _LOGGER.info("%s did not come back within %s; its page was not reloaded", entity_id, RELOAD_WAIT)

    unsubs.append(async_track_state_change_event(hass, [entity_id], _changed))
    unsubs.append(async_call_later(hass, RELOAD_WAIT, _give_up))


async def async_reload_portals(hass: HomeAssistant) -> None:
    for device in fleet.device_entries(hass):
        if not _is_portal(device):
            continue
        entity_id = await _reload_button(hass, device)
        if entity_id is None:
            _LOGGER.debug("%s has no linked Reload page button; skipped", device.title)
        elif _available(hass.states.get(entity_id)):
            await _press(hass, entity_id)
        else:
            _press_when_available(hass, entity_id)


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Arm the reload for this HA start; a KSM set up after start does nothing."""
    if hass.state is CoreState.running:
        return

    async def _started(_event: Event) -> None:
        await async_reload_portals(hass)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started)
