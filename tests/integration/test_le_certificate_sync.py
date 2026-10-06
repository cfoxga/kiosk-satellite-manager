"""[KSM-TEST-353] Renewal sync uses the existing pin and updates only on success."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import acme_renewal, le_certificate, le_certificate_sync
from custom_components.kiosk_satellite_manager.const import (
    CONF_HOST, CONF_PASSWORD, CONF_TLS_SPKI, DOMAIN,
    CONF_LE_CERTIFICATE_HOSTNAME, CONF_LE_CERTIFICATE_FINGERPRINT,
)
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError
from unittest.mock import MagicMock
from homeassistant.helpers import issue_registry as ir

from .conftest import init_integration


@pytest.fixture(autouse=True)
def probe():
    """The device is not yet serving HA's new certificate unless a test says so."""
    with patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.probe_https_identity",
        new=AsyncMock(return_value=None),
    ) as probed:
        yield probed


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


async def test_legacy_device_syncs_on_setup_and_manager_tick(hass):
    """[KSM-TEST-353/408] Without Certificates settings a legacy device keeps
    the `/ssl` sync: on its setup and on each manager tick, and a failing
    sync raises its repair until one succeeds."""
    sync = AsyncMock(return_value=False)
    with patch.object(le_certificate_sync, "async_sync_device", new=sync), patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.90"}),
    ):
        ctx = await init_integration(hass, data={
            CONF_TLS_SPKI: "aa" * 32,
            CONF_LE_CERTIFICATE_HOSTNAME: "test-portal-mini.cfoxga.com",
            CONF_LE_CERTIFICATE_FINGERPRINT: "old",
        })
        await hass.async_block_till_done(wait_background_tasks=True)
        assert sync.await_count == 1
        sync.side_effect = le_certificate.CertificateUnavailable("certificate expired")
        await acme_renewal.async_tick(hass)
        assert sync.await_count == 2
        issue_id = f"le_certificate_sync_failed_{ctx.entry.entry_id}"
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
        sync.side_effect = None
        await acme_renewal.async_tick(hass)
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
        await hass.config_entries.async_unload(ctx.entry.entry_id)
        await acme_renewal.async_tick(hass)
        assert sync.await_count == 3, "an unloaded device is not synced"


def _raise_pin_repair(hass, device):
    ir.async_create_issue(
        hass, DOMAIN, f"tls_certificate_changed_{device.entry_id}",
        is_fixable=True, severity=ir.IssueSeverity.ERROR,
        translation_key="tls_certificate_changed",
        translation_placeholders={"name": "Mini", "host": "192.0.2.61"},
        data={"entry_id": device.entry_id},
    )


async def test_device_already_serving_ha_certificate_is_adopted(hass, device, probe):
    """[KSM-TEST-354] An import that landed after its check gave up is adopted, not redone."""
    selected = le_certificate.CertificateMaterial("cert", "key", "bb" * 32, "new")
    probe.return_value = (selected.spki_sha256, selected.fingerprint)
    _raise_pin_repair(hass, device)
    coordinator = MagicMock(async_request_refresh=AsyncMock())
    hass.data.setdefault(DOMAIN, {})[device.entry_id] = coordinator
    imported = AsyncMock()
    with patch.object(le_certificate, "load_for_hostname", return_value=selected), patch(
        "custom_components.kiosk_satellite_manager.ks_tls.async_import_certificate", new=imported
    ), patch(
        "custom_components.kiosk_satellite_manager.ks_api_client.login", new=AsyncMock()
    ) as login:
        assert await le_certificate_sync.async_sync_device(hass, device) is True
    imported.assert_not_awaited()
    login.assert_not_awaited()
    assert probe.await_args.args[1] == "192.0.2.61"
    assert device.data[CONF_TLS_SPKI] == "bb" * 32
    assert device.data[CONF_LE_CERTIFICATE_FINGERPRINT] == "new"
    issue_id = f"tls_certificate_changed_{device.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.parametrize("served", [
    ("aa" * 32, "old"),   # still the previous certificate
    ("bb" * 32, "other"),  # HA's key, but not HA's current certificate
    ("cc" * 32, "new"),   # HA's certificate fingerprint on another key
    None,                  # no HTTPS answer
])
async def test_device_not_serving_ha_certificate_imports_over_old_pin(
    hass, device, probe, served
):
    """[KSM-TEST-354] Negative: anything short of an exact match imports as before."""
    selected = le_certificate.CertificateMaterial("cert", "key", "bb" * 32, "new")
    probe.return_value = served
    _raise_pin_repair(hass, device)
    imported = AsyncMock(return_value=selected.spki_sha256)
    with patch.object(le_certificate, "load_for_hostname", return_value=selected), patch(
        "custom_components.kiosk_satellite_manager.ks_tls.async_import_certificate", new=imported
    ):
        assert await le_certificate_sync.async_sync_device(hass, device) is True
    assert imported.await_args.args[1:4] == ("192.0.2.61", "secret", "aa" * 32)
    issue_id = f"tls_certificate_changed_{device.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
