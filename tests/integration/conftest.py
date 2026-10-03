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
def _permissions_adb_unreachable():
    """The permissions poll (KSM-BEHAVE-141) must never open a real socket."""
    from custom_components.kiosk_satellite_manager.adb_client import AdbConnectFailed

    client = AsyncMock()
    client.connect = AsyncMock(side_effect=AdbConnectFailed("test: no device"))
    with patch("custom_components.kiosk_satellite_manager.permissions.AdbClient", return_value=client):
        yield


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow HA to load the custom kiosk_satellite_manager integration in
    every integration test."""
    yield


@pytest.fixture(autouse=True)
def legacy_entry_suite_compat(request):
    """Keep pre-fleet tests on their original per-entry setup contract.

    test_fleet_entries.py exercises the real migration and native ownership.
    The older tests remain valuable for each physical device's behavior.
    """
    if request.node.path.name == "test_fleet_entries.py":
        yield
        return
    with patch("custom_components.kiosk_satellite_manager.fleet.async_migrate_legacy_device",
               new=AsyncMock()):
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
    ) as mock, patch(
        "custom_components.kiosk_satellite_manager.async_check_device_for_update",
        new=AsyncMock(return_value="sees 2026.9.1"),
    ):
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
def ks_setup_status():
    """Keep the first-run password probe off the network: by default the
    device already has an admin password."""
    with patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.get_setup_status",
        new=AsyncMock(return_value={"setupNeeded": False, "passwordNeeded": False}),
    ) as status, patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.setup_password",
        new=AsyncMock(return_value="tok"),
    ):
        yield status


@pytest.fixture(autouse=True)
def tls_migration():
    """KSM-BEHAVE-169 (#137): only Use HTTPS and Install on a pinned entry
    switch a device to HTTPS. Keep any such call off the network; None models
    a KS that predates TLS, and tests assert the call never happens where
    HTTPS is not opted into."""
    with patch(
        "custom_components.kiosk_satellite_manager.ks_tls.async_establish_tls",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def ks_connect_ha():
    """KSM-BEHAVE-163: the ADB-free add writes the kiosk's HA settings over
    its API. Keep it off the network; `test_config_flow.py`'s KSM-TEST-326/327
    put the real helper back and stub only the KS API calls under it."""
    from custom_components.kiosk_satellite_manager.credentials import TokenCredential

    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.async_connect_ha",
        new=AsyncMock(return_value=TokenCredential("minted-token", "refresh-id", owned=True)),
        create=True,
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def esphome_identity():
    """KSM-BEHAVE-110: each device setup fills an empty ESPHome node name over
    the KS API in the background. Keep it off the network;
    `test_esphome_identity.py` puts the real function back."""
    with patch(
        "custom_components.kiosk_satellite_manager.async_ensure_esphome_identity",
        new=AsyncMock(),
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
