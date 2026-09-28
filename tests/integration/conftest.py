"""Fixtures for Kiosk Satellite Manager integration tests
(pytest-homeassistant-custom-component). Mirrors ham-notify's
tests/integration/conftest.py pattern.
"""
from __future__ import annotations

pytest_plugins = ["pytest_homeassistant_custom_component"]

import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, patch

# tests/integration/ -> tests/ -> kiosk-satellite-manager/ (parent of custom_components/)
_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from custom_components.kiosk_satellite_manager.const import (  # noqa: E402
    CONF_HOST,
    CONF_KEY_PATH,
    CONF_PASSWORD,
    CONF_PORT,
    DOMAIN,
)
from custom_components.kiosk_satellite_manager.ks_api import ReleaseInfo  # noqa: E402
from homeassistant.auth.const import GROUP_ID_ADMIN  # noqa: E402
from homeassistant.core import Context  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow HA to load the custom kiosk_satellite_manager integration in
    every integration test."""
    yield


@pytest.fixture(autouse=True)
def release_check():
    """KSM-BEHAVE-071: every entry setup starts the shared GitHub release
    check. Keep it off the network (phacc blocks sockets) and let a test
    swap the result via `release_check.return_value`."""
    mock = AsyncMock(
        return_value=ReleaseInfo("2026.9.1", "https://example.invalid/releases/2026.9.1", "notes")
    )
    with patch("custom_components.kiosk_satellite_manager.latest_release_info", new=mock):
        yield mock


@pytest.fixture(autouse=True)
def apk_upload(tmp_path):
    """KSM-BEHAVE-107/108: a self-update takes the APK from KSM's cache and
    uploads it. The cache hands back a real small file (no GitHub), and the
    upload stub drains the streamed body into `received` so a test sees what
    the device would have been sent. Override `upload.return_value`-style via
    `apk_upload.reply`."""
    apk = tmp_path / "kiosk-satellite-2026.9.77.arm64-v8a.apk"
    apk.write_bytes(b"cached-apk-" * 1000)
    state = SimpleNamespace(
        path=apk,
        received=[],
        reply={"ok": True, "data": {"buildNumber": 2, "currentBuild": 1}},
    )

    async def upload(session, host, token, body, size, *, pin=None):
        data = b"".join([chunk async for chunk in body])
        state.received.append({"host": host, "token": token, "size": size, "body": data})
        return state.reply

    state.release_apk = AsyncMock(return_value=apk)
    state.upload = AsyncMock(side_effect=upload)
    state.prune = AsyncMock(return_value=[])
    prefix = "custom_components.kiosk_satellite_manager.ks_update."
    with patch(prefix + "apk_cache.async_release_apk", new=state.release_apk), patch(
        prefix + "apk_cache.async_prune", new=state.prune
    ), patch(prefix + "ks_api_client.upload_update", new=state.upload):
        yield state


@pytest.fixture(autouse=True)
def device_update_check():
    """KSM-BEHAVE-103: a newly seen release fans `checkUpdateNow` out to every
    device over its API. Keep that off the network; tests assert on the mock."""
    with patch(
        "custom_components.kiosk_satellite_manager.async_check_devices_for_update",
        new=AsyncMock(return_value={}),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def adb_probe():
    """KSM-BEHAVE-079: the ADB-enabled binary sensor polls a TCP connect to
    the entry's host. Keep it off the network; tests that care patch the
    same target themselves."""
    with patch(
        "custom_components.kiosk_satellite_manager.binary_sensor.async_probe_adb_port",
        new=AsyncMock(return_value=False),
        create=True,
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def ks_health_probe():
    """KSM-BEHAVE-096: Add Device asks Kiosk Satellite's health endpoint
    before ADB. Default: nothing answers, so the ADB flow runs as before.
    ADB-free tests set `ks_health_probe.return_value = (pin, health)`."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow._async_probe_ks_health",
        new=AsyncMock(return_value=None),
        create=True,
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def tls_migration():
    """KSM-BEHAVE-094: setup of an unpinned entry with a password switches
    the device to HTTPS in the background. Keep it off the network; a KS that
    predates TLS (None) leaves the entry unchanged. `test_tls_migration.py`
    patches the same target itself."""
    with patch(
        "custom_components.kiosk_satellite_manager.ks_tls.async_establish_tls",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


@dataclass
class KSMContext:
    entry: MockConfigEntry


async def init_integration(
    hass, *, data: dict | None = None, options: dict | None = None
) -> KSMContext:
    entry_data = {
        CONF_HOST: "192.168.99.99",
        CONF_PORT: 5555,
        CONF_KEY_PATH: "/tmp/ksm-test-key/adbkey",
        CONF_PASSWORD: "synthetic-test-password",
    }
    entry_data.update(data or {})
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, options=options or {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return KSMContext(entry=entry)


async def admin_context(hass) -> Context:
    """Create an authenticated HA administrator service-call context."""
    user = await hass.auth.async_create_user("KSM service test admin", group_ids=[GROUP_ID_ADMIN])
    return Context(user_id=user.id)
