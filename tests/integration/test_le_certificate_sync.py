"""[KSM-TEST-353] Renewal sync uses the existing pin and updates only on success."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import le_certificate, le_certificate_sync
from custom_components.kiosk_satellite_manager.const import (
    CONF_HOST, CONF_PASSWORD, CONF_TLS_SPKI, DOMAIN,
    CONF_LE_CERTIFICATE_HOSTNAME, CONF_LE_CERTIFICATE_FINGERPRINT,
)
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError
from unittest.mock import MagicMock

import custom_components.kiosk_satellite_manager as integration
from homeassistant.helpers import issue_registry as ir

from .conftest import init_integration


@pytest.fixture
def device(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="Mini", unique_id="mini", data={
        CONF_HOST: "192.0.2.61", CONF_PASSWORD: "secret", CONF_TLS_SPKI: "aa" * 32,
        CONF_LE_CERTIFICATE_HOSTNAME: "test-portal-mini.cfoxga.com",
        CONF_LE_CERTIFICATE_FINGERPRINT: "old",
    })
    entry.add_to_hass(hass)
    return entry


async def test_changed_certificate_imports_over_old_pin_then_updates(hass, device):
    selected = le_certificate.CertificateMaterial("cert", "key", "bb" * 32, "new")
    imported = AsyncMock(return_value=selected.spki_sha256)
    with patch.object(le_certificate, "load_for_hostname", return_value=selected), patch(
        "custom_components.kiosk_satellite_manager.ks_tls.async_import_certificate", new=imported
    ):
        assert await le_certificate_sync.async_sync_device(hass, device) is True
    assert imported.await_args.args[1:4] == ("192.0.2.61", "secret", "aa" * 32)
    assert device.data[CONF_TLS_SPKI] == "bb" * 32
    assert device.data[CONF_LE_CERTIFICATE_FINGERPRINT] == "new"


async def test_unchanged_certificate_does_not_import(hass, device):
    selected = le_certificate.CertificateMaterial("cert", "key", "bb" * 32, "old")
    imported = AsyncMock()
    with patch.object(le_certificate, "load_for_hostname", return_value=selected), patch(
        "custom_components.kiosk_satellite_manager.ks_tls.async_import_certificate", new=imported
    ):
        assert await le_certificate_sync.async_sync_device(hass, device) is False
    imported.assert_not_awaited()


async def test_import_failure_keeps_old_pin_and_fingerprint(hass, device):
    selected = le_certificate.CertificateMaterial("cert", "key", "bb" * 32, "new")
    imported = AsyncMock(side_effect=KsApiError("import rejected"))
    with patch.object(le_certificate, "load_for_hostname", return_value=selected), patch(
        "custom_components.kiosk_satellite_manager.ks_tls.async_import_certificate", new=imported
    ):
        with pytest.raises(KsApiError):
            await le_certificate_sync.async_sync_device(hass, device)
    assert device.data[CONF_TLS_SPKI] == "aa" * 32
    assert device.data[CONF_LE_CERTIFICATE_FINGERPRINT] == "old"


async def test_device_without_certificate_selection_is_not_synced(hass, device):
    hass.config_entries.async_update_entry(device, data={
        k: v for k, v in device.data.items() if k != CONF_LE_CERTIFICATE_HOSTNAME
    })
    with patch.object(le_certificate, "load_for_hostname") as load:
        assert await le_certificate_sync.async_sync_device(hass, device) is False
    load.assert_not_called()


async def test_setup_and_timer_sync_only_until_unload(hass):
    """[KSM-TEST-353] The scheduler actually calls renewal sync and is removed on unload."""
    sync = AsyncMock(return_value=False)
    callbacks = []
    cancel = MagicMock()
    original = integration.async_track_time_interval

    def track(hass_arg, callback, interval, *, name):
        if "certificate_sync" in name:
            callbacks.append(callback)
            return cancel
        return original(hass_arg, callback, interval, name=name)

    with patch.object(integration, "async_track_time_interval", new=track), patch.object(
        le_certificate_sync, "async_sync_device", new=sync
    ), patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.90"}),
    ):
        ctx = await init_integration(hass, data={
            CONF_TLS_SPKI: "aa" * 32,
            CONF_LE_CERTIFICATE_HOSTNAME: "test-portal-mini.cfoxga.com",
            CONF_LE_CERTIFICATE_FINGERPRINT: "old",
        })
        assert sync.await_count == 1
        assert len(callbacks) == 1
        sync.side_effect = le_certificate.CertificateUnavailable("certificate expired")
        await callbacks[0]()
        assert sync.await_count == 2
        issue_id = f"le_certificate_sync_failed_{ctx.entry.entry_id}"
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
        sync.side_effect = None
        await callbacks[0]()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
        await hass.config_entries.async_unload(ctx.entry.entry_id)
        cancel.assert_called_once()
        assert sync.await_count == 3
