"""KSM sensors: per-device IP address, device type, install recipe and fleet
membership; fleet status; and the manager's device catalog and latest
release. There is no installed-version sensor (#95) -- ESPHome's update
entity reports the installed Kiosk Satellite version.

KSM-BEHAVE-009: the device's suggested Area (chosen at config-flow time)
carries through to DeviceInfo.suggested_area here too, not just on the
button, since HA only applies device-registry defaults the first time
either entity registers the device.
"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_AREA_ID, CONF_DEVICE_PROFILE, CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_MANAGER,
    ENTRY_TYPE_UNMANAGED, ENTRY_TYPE_FLEET, RELEASE_COORDINATOR_KEY,
)
from .device_catalog import CATALOG, NoApprovedRecipe, require_recipe
from .device_models import DEVICE_MODELS, get_device_model
from .install_recipes import INSTALL_RECIPES, InstallRecipe
from .helpers import resolve_area_name
from . import fleet

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER:
        async_add_entities([
            KioskSatelliteLatestReleaseSensor(hass, entry),
            KioskSatelliteCatalogSensor(entry, "models"),
            KioskSatelliteCatalogSensor(entry, "recipes"),
        ])
        return
    if entry.data.get(CONF_ENTRY_TYPE) in (ENTRY_TYPE_UNMANAGED, ENTRY_TYPE_FLEET):
        async_add_entities([FleetStatusSensor(hass, entry, kind) for kind in (
            ("managed_count",) if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_UNMANAGED
            else ("leader", "managed_count", "online_count", "offline_count",
                  "version_mismatch", "blocked_sync", "pending_invitations", "last_poll")
        )])
    for device in fleet.platform_devices(hass, entry):
        coordinator = hass.data[DOMAIN][device.entry_id]
        fleet.add_entities(async_add_entities, device, [
            KioskSatelliteIpAddressSensor(hass, coordinator, device),
            KioskSatelliteDeviceTypeSensor(hass, device),
            KioskSatelliteRecipeSensor(hass, device),
            FleetMembershipSensor(hass, device),
        ])


class FleetMembershipSensor(SensorEntity):
    """A managed kiosk's accepted KS membership or external leader state."""

    _attr_has_entity_name = True
    _attr_name = "Fleet membership"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry | fleet.DeviceEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_fleet_membership"
        self._attr_device_info = _device_info(hass, entry)

    @property
    def native_value(self) -> str | None:
        if not isinstance(self.entry, fleet.DeviceEntry):
            return None
        status = self.entry.fleet_status
        if not status:
            return None
        if not fleet.status_available(self.hass, self.entry.entry_id):
            return "stale"
        if status.get("leading"):
            return "leader"
        leader_id = status.get("following_id")
        if leader_id:
            return "following" if self.entry.parent.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_FLEET else "external leader"
        return "unmanaged"


class FleetStatusSensor(SensorEntity):
    """Native HA status for a KSM grouping, derived from confirmed device reads."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, kind: str) -> None:
        self.hass = hass
        self.entry = entry
        self.kind = kind
        self._attr_unique_id = f"{entry.entry_id}_fleet_{kind}"
        self._attr_name = kind.replace("_", " ").title()

    @property
    def native_value(self):
        members = fleet.device_entries(self.hass, self.entry)
        if self.kind == "managed_count":
            return len(members)
        leader = next((item for item in members if item.fleet_status.get("leading")
                       and item.fleet_status.get("self_id") == self.entry.data.get("leader_id")), None)
        if self.kind == "leader":
            return leader.title if leader else None
        if leader is None:
            return None
        if self.kind == "last_poll":
            return leader.fleet_status.get("observed_at")
        coordinator = self.hass.data.get(DOMAIN, {}).get(leader.entry_id)
        if (coordinator is None or not coordinator.last_update_success
                or not fleet.status_available(self.hass, leader.entry_id)):
            return None
        if self.kind in ("online_count", "offline_count"):
            states = [self.hass.data.get(DOMAIN, {}).get(item.entry_id) for item in members]
            online = sum(1 for state in states if state and state.last_update_success)
            return online if self.kind == "online_count" else len(members) - online
        if self.kind == "pending_invitations":
            return leader.fleet_status.get("pending_invitations")
        rows = leader.fleet_status.get("follower_rows")
        if not isinstance(rows, dict):
            return None
        managed_followers = [item for item in members if item.entry_id != leader.entry_id]
        if any(item.fleet_status.get("self_id") not in rows for item in managed_followers):
            return None
        followers = [rows[item.fleet_status["self_id"]] for item in managed_followers]
        if any(not isinstance(row, dict) or row.get("phase") is None for row in followers):
            return None
        if self.kind == "version_mismatch":
            return sum(1 for row in followers if row.get("phase") == "version")
        if self.kind == "blocked_sync":
            return sum(1 for row in followers if row.get("phase") in ("error", "version"))
        return None


def _device_info(hass: HomeAssistant, entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        suggested_area=resolve_area_name(hass, entry.data.get(CONF_AREA_ID)),
    )


class KioskSatelliteIpAddressSensor(CoordinatorEntity, SensorEntity):
    """KSM-BEHAVE-079: the device's own reported IP, from /api/health only."""

    _attr_has_entity_name = True
    _attr_name = "IP address"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_ip_address"
        self._attr_device_info = _device_info(hass, entry)

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("ip") if self.coordinator.data else None


class KioskSatelliteDeviceTypeSensor(SensorEntity):
    """KSM-BEHAVE-079: the exact catalog model stored at setup, never re-detected."""

    _attr_has_entity_name = True
    _attr_name = "Device type"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        model_key = entry.data.get(CONF_DEVICE_PROFILE)
        model = get_device_model(model_key)
        self._attr_unique_id = f"{entry.entry_id}_device_type"
        self._attr_device_info = _device_info(hass, entry)
        self._attr_native_value = model.name if model else model_key
        self._attr_extra_state_attributes = {"model_key": model_key}


class KioskSatelliteRecipeSensor(SensorEntity):
    """KSM-BEHAVE-079: the recipe the install executor will run for this model.

    Resolved through require_recipe, the executor's own entry point, so the
    sensor can never name a recipe an install would refuse.
    """

    _attr_has_entity_name = True
    _attr_name = "Install recipe"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_recipe"
        self._attr_device_info = _device_info(hass, entry)
        try:
            recipe = require_recipe(entry.data.get(CONF_DEVICE_PROFILE))
        except NoApprovedRecipe as err:
            self._attr_native_value = "none"
            self._attr_extra_state_attributes = {"reason": str(err)}
            return
        self._attr_native_value = recipe.recipe_key
        self._attr_extra_state_attributes = {
            "recipe_key": recipe.recipe_key,
            "recipe_name": recipe.name,
            **_recipe_details(recipe),
        }


def _recipe_details(recipe: InstallRecipe) -> dict:
    """Expose only typed, source-maintained policy; never live KS settings."""
    return {
        "artifact_policy": recipe.artifact_policy,
        "device_name_source": recipe.device_name_source,
        "start_url_path": recipe.start_url_path,
        "install_strategy": recipe.install_strategy,
        "update_strategy": recipe.update_strategy,
        "reinstall_strategy": recipe.reinstall_strategy,
        "uninstall_behavior": recipe.uninstall_behavior,
        "permission_policy": recipe.permission_policy,
        "appops_policy": recipe.appops_policy,
        "repurpose_policy": recipe.repurpose_policy,
        "battery_exemption": recipe.battery_exemption,
        "sets_device_admin": recipe.sets_device_admin,
        "verifier_retry_on_failure": recipe.verifier_retry_on_failure,
        "operations": list(recipe.operations),
        "parameters": dict(recipe.parameters),
        "postconditions": list(recipe.postconditions),
    }


class KioskSatelliteCatalogSensor(SensorEntity):
    """Read-only catalog browser on the native manager entry."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, kind: str) -> None:
        self._attr_unique_id = f"{entry.entry_id}_catalog_{kind}"
        self._attr_name = "Device catalog" if kind == "models" else "Install recipes"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )
        if kind == "models":
            rows = []
            for model in DEVICE_MODELS:
                try:
                    recipe = require_recipe(model.model_key)
                except NoApprovedRecipe:
                    recipe = None
                rows.append({
                    "model_key": model.model_key,
                    "model_name": model.name,
                    "recipe_key": recipe.recipe_key if recipe else None,
                    "qualification_records": sum(
                        q.model_key == model.model_key and recipe is not None
                        and q.recipe_key == recipe.recipe_key
                        for q in CATALOG.qualifications
                    ),
                })
            self._attr_extra_state_attributes = {"models": rows}
        else:
            self._attr_extra_state_attributes = {"recipes": [
                {
                    "recipe_key": recipe.recipe_key,
                    "recipe_name": recipe.name,
                    "assigned_models": [
                        model.model_key for model in DEVICE_MODELS
                        if _model_has_recipe(model.model_key, recipe.recipe_key)
                    ],
                    **_recipe_details(recipe),
                }
                for recipe in INSTALL_RECIPES
            ]}
        self._attr_native_value = len(rows) if kind == "models" else len(INSTALL_RECIPES)


def _model_has_recipe(model_key: str, recipe_key: str) -> bool:
    try:
        return require_recipe(model_key).recipe_key == recipe_key
    except NoApprovedRecipe:
        return False


class KioskSatelliteLatestReleaseSensor(CoordinatorEntity, SensorEntity):
    """KSM-wide release status, independent of any device's health."""

    _attr_has_entity_name = True
    _attr_name = "Latest Kiosk Satellite release"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass.data[RELEASE_COORDINATOR_KEY])
        self._attr_unique_id = f"{entry.entry_id}_latest_release"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.version if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict:
        release = self.coordinator.data
        return {
            "release_url": release.url if release else None,
            "notes": release.notes if release else None,
            "last_successful_check": (
                self.coordinator.ksm_last_success.isoformat()
                if getattr(self.coordinator, "ksm_last_success", None) else None
            ),
            "check_status": "current" if self.coordinator.last_update_success else "stale",
        }

    async def async_update(self) -> None:
        await self.coordinator.async_request_refresh()
