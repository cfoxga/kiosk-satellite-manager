"""Opt-in auto-update (KSM-BEHAVE-073, KSM-BEHAVE-134).

KSM has no update entity: ESPHome's is the operator-facing update surface.
This is the per-device auto-update logic that used to live in that entity.
Installed version is the health coordinator's `appVersion`; target is
`helpers.target_release` (the pinned Install version, else the latest
release). Evaluated on every health or release coordinator update, when the
entry's options change, and on the Auto-update all / manager option signals.
It fires only on a reachable device with no install running and an older
`appVersion`, at most once per version per load of the device, so a release
that fails to install is not retried on every poll.
"""
from __future__ import annotations

import logging

from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    CONF_AUTO_UPDATE, DOMAIN, RELEASE_COORDINATOR_KEY, SIGNAL_AUTO_UPDATE_ALL,
    SIGNAL_MANAGER_OPTIONS_UPDATED,
)
from .helpers import auto_update_all_enabled, target_release
from .ks_update import async_self_update_entry, notify_awaiting_confirmation

_LOGGER = logging.getLogger(__name__)


def is_older(installed: str | None, target: str | None) -> bool:
    """True when a known installed version is older than the target."""
    if not installed or not target:
        return False
    try:
        return AwesomeVersion(installed) < AwesomeVersion(target)
    except AwesomeVersionException:
        return False


@callback
def async_setup(hass: HomeAssistant, entry, health):
    """Start auto-update for one device; returns the callback that stops it."""
    attempted: set[str] = set()

    @callback
    def _evaluate(*_args) -> None:
        if not (entry.options.get(CONF_AUTO_UPDATE, False) or auto_update_all_enabled(hass)):
            return
        if not health.last_update_success or health.ksm_installing:
            return
        target = target_release(hass)
        installed = (health.data or {}).get("appVersion")
        if target is None or not is_older(installed, target.version):
            return
        version = target.version
        if version in attempted:
            return
        attempted.add(version)
        _LOGGER.info(
            "auto-updating Kiosk Satellite on %s from %s to %s", entry.title, installed, version
        )
        entry.async_create_background_task(
            hass, _install(version), f"{DOMAIN} auto-update {entry.entry_id}"
        )

    async def _install(version: str) -> None:
        try:
            notify_awaiting_confirmation(hass, entry, await async_self_update_entry(hass, entry))
        except Exception as err:  # logged, never retried for this version
            _LOGGER.warning(
                "automatic Kiosk Satellite update to %s failed on %s: %s", version, entry.title, err
            )

    opted_in = bool(entry.options.get(CONF_AUTO_UPDATE, False))

    async def _options_changed(_hass: HomeAssistant, _entry) -> None:
        # Credential or other data edits also notify; only the opt-in matters.
        nonlocal opted_in
        now = bool(entry.options.get(CONF_AUTO_UPDATE, False))
        if now != opted_in:
            opted_in = now
            _evaluate()

    unsubs = [health.async_add_listener(_evaluate)]
    release = hass.data.get(RELEASE_COORDINATOR_KEY)
    if release is not None:
        unsubs.append(release.async_add_listener(_evaluate))
    unsubs.append(entry.add_update_listener(_options_changed))
    unsubs.append(async_dispatcher_connect(hass, SIGNAL_AUTO_UPDATE_ALL, _evaluate))
    unsubs.append(async_dispatcher_connect(hass, SIGNAL_MANAGER_OPTIONS_UPDATED, _evaluate))
    _evaluate()

    @callback
    def _stop() -> None:
        while unsubs:
            unsubs.pop()()

    return _stop
