"""Permission status readback and fix (KSM-BEHAVE-141, #102).

Status always comes from the device's own readback. A required permission
the installed app does not declare cannot be granted, so it is reported
separately and never counts as a problem.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .adb_client import AdbAuthPending, AdbClient, AdbConnectFailed
from .const import CONF_DEVICE_PROFILE, CONF_HOST, CONF_KEY_PATH, CONF_PORT, PERMISSIONS_MONITOR_KEY
from .device_catalog import NoApprovedRecipe, require_recipe
from .install import grant_recipe_permissions
from .install_recipes import InstallRecipe

BATTERY_EXEMPTION = "battery_exemption"
_UNREACHABLE = (AdbConnectFailed, AdbAuthPending, OSError)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PermissionStatus:
    missing: tuple[str, ...]
    not_declared: tuple[str, ...]

    @property
    def problem(self) -> bool:
        return bool(self.missing)


@dataclass
class PermissionMonitor:
    """Last known status for one device, shared by its sensor and button."""

    status: PermissionStatus | None = None
    listeners: list[Callable[[], None]] = field(default_factory=list)

    def update(self, status: PermissionStatus) -> None:
        self.status = status
        for listener in list(self.listeners):
            listener()


def get_monitor(hass: HomeAssistant, entry: ConfigEntry) -> PermissionMonitor:
    return hass.data.setdefault(PERMISSIONS_MONITOR_KEY, {}).setdefault(
        entry.entry_id, PermissionMonitor()
    )


def approved_recipe(entry: ConfigEntry) -> InstallRecipe | None:
    try:
        return require_recipe(entry.data.get(CONF_DEVICE_PROFILE))
    except NoApprovedRecipe:
        return None


async def read_status(client: AdbClient, sdk: int, recipe: InstallRecipe) -> PermissionStatus:
    declared = await client.declared_permissions()
    granted = await client.granted_permissions()
    missing: list[str] = []
    not_declared: list[str] = []
    for perm in recipe.permissions_for_sdk(sdk):
        if perm not in declared:
            not_declared.append(perm.removeprefix("android.permission."))
        elif perm not in granted:
            missing.append(perm.removeprefix("android.permission."))
    for op in recipe.appops_for_sdk(sdk):
        if await client.appop_mode(op) != "allow":
            missing.append(f"appop:{op}")
    if recipe.battery_exemption and not await client.is_battery_exempt():
        missing.append(BATTERY_EXEMPTION)
    return PermissionStatus(tuple(missing), tuple(not_declared))


async def _sdk(client: AdbClient) -> int:
    value = (await client.getprop("ro.build.version.sdk")).strip()
    return int(value) if value.isdigit() else 29


async def _run(entry: ConfigEntry, recipe: InstallRecipe, *, fix: bool) -> PermissionStatus:
    client = AdbClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_KEY_PATH])
    await client.connect()
    try:
        sdk = await _sdk(client)
        if fix:
            await grant_recipe_permissions(client, sdk, recipe)
        return await read_status(client, sdk, recipe)
    finally:
        await client.close()


async def async_refresh(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Poll: an unreachable device leaves the last known status untouched."""
    recipe = approved_recipe(entry)
    if recipe is None:
        return
    try:
        status = await _run(entry, recipe, fix=False)
    except _UNREACHABLE as err:
        _LOGGER.debug("permission poll skipped for %s: %r", entry.title, err)
        return
    get_monitor(hass, entry).update(status)


async def async_fix(hass: HomeAssistant, entry: ConfigEntry) -> PermissionStatus:
    recipe = approved_recipe(entry)
    if recipe is None:
        raise HomeAssistantError(f"{entry.title} has no approved install recipe")
    try:
        status = await _run(entry, recipe, fix=True)
    except _UNREACHABLE as err:
        raise HomeAssistantError(
            f"Kiosk Satellite ADB is unreachable at {entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"
            " -- ADB is needed to fix permissions"
        ) from err
    get_monitor(hass, entry).update(status)
    return status
