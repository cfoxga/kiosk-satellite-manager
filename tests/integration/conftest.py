"""Fixtures for Kiosk Satellite Manager integration tests
(pytest-homeassistant-custom-component). Mirrors ham-notify's
tests/integration/conftest.py pattern.
"""
from __future__ import annotations

pytest_plugins = ["pytest_homeassistant_custom_component"]

import os
import sys
from dataclasses import dataclass

import pytest

# tests/integration/ -> tests/ -> kiosk-satellite-manager/ (parent of custom_components/)
_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from custom_components.kiosk_satellite_manager.const import (  # noqa: E402
    CONF_HOST,
    CONF_KEY_PATH,
    CONF_PORT,
    DOMAIN,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow HA to load the custom kiosk_satellite_manager integration in
    every integration test."""
    yield


@dataclass
class KSMContext:
    entry: MockConfigEntry


async def init_integration(hass, *, data: dict | None = None) -> KSMContext:
    entry_data = {
        CONF_HOST: "192.168.99.99",
        CONF_PORT: 5555,
        CONF_KEY_PATH: "/tmp/ksm-test-key/adbkey",
    }
    entry_data.update(data or {})
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return KSMContext(entry=entry)
