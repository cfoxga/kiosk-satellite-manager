"""KSM-TEST-341/346 (#140): Uninstall on a Device Owner device explains that a
factory reset is required and how to do it by hand. KSM offers no in-app
reset (KSM-BEHAVE-172 retired).

Only the ADB transport is faked: the button, issue registry and repairs
platform are real.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest

from custom_components.kiosk_satellite_manager import repairs
from custom_components.kiosk_satellite_manager.adb_client import UninstallDeviceOwner
from custom_components.kiosk_satellite_manager.const import CONF_DEVICE_PROFILE, DOMAIN

from .conftest import init_integration

_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_BUTTON_ADB = "custom_components.kiosk_satellite_manager.button.AdbClient"
_STRINGS = Path(repairs.__file__).with_name("strings.json")
_STEPS = ("Volume Up", "Volume Down", "10 seconds")


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"factory_reset_{entry.entry_id}")


async def _setup(hass, profile="portal_gen1"):
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.91"})):
        return (await init_integration(hass, data={CONF_DEVICE_PROFILE: profile})).entry


async def _press_uninstall(hass, entry, uninstall: AsyncMock):
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_uninstall"
    )
    with patch(_BUTTON_ADB) as client_cls:
        client = client_cls.return_value
        client.connect = AsyncMock()
        client.uninstall_ks = uninstall
        client.close = AsyncMock()
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
    return client


# --- KSM-TEST-341: the button and its repair ---------------------------------

@pytest.mark.parametrize("profile", ["portal_gen1", "portal_mini", "portal_go"])
async def test_KSM_TEST_341_device_owner_says_reset_by_hand(hass, profile):
    entry = await _setup(hass, profile)
    with pytest.raises(HomeAssistantError, match=r"Device Owner.*factory reset") as err:
        await _press_uninstall(hass, entry, AsyncMock(side_effect=UninstallDeviceOwner()))
    message = str(err.value)
    assert "DELETE_FAILED" not in message
    for step in _STEPS:
        assert step in message
    issue = _issue(hass, entry)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.translation_key == "factory_reset"
    assert issue.translation_placeholders == {"name": entry.title}


def test_KSM_TEST_341_repair_text_carries_the_manual_steps():
    issue = json.loads(_STRINGS.read_text(encoding="utf-8"))["issues"]["factory_reset"]
    assert "fix_flow" not in issue
    for step in _STEPS:
        assert step in issue["description"]


async def test_KSM_TEST_341_a_successful_uninstall_clears_the_repair(hass):
    """Negative: no Device Owner, no repair -- and an old one goes away."""
    entry = await _setup(hass)
    with pytest.raises(HomeAssistantError):
        await _press_uninstall(hass, entry, AsyncMock(side_effect=UninstallDeviceOwner()))
    assert _issue(hass, entry) is not None
    await _press_uninstall(hass, entry, AsyncMock())
    assert _issue(hass, entry) is None


# --- KSM-TEST-346: no in-app reset remains ------------------------------------

def test_KSM_TEST_346_no_factory_reset_flow_or_text():
    assert hasattr(repairs, "DeviceSupportFlow"), "positive control: the real module"
    assert not hasattr(repairs, "FactoryResetFlow")
    issues = json.loads(_STRINGS.read_text(encoding="utf-8"))["issues"]
    assert "device_support" in issues, "positive control: the real strings"
    assert "factory_reset_manual" not in issues
