"""KSM-TEST-020: capability-report service wiring."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.kiosk_satellite_manager.const import DOMAIN
from .conftest import init_integration


async def test_capability_report_returns_the_sanitized_report_and_closes_client(hass):
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})):
        ctx = await init_integration(hass)
    report = {"schema_version": 1, "facts": {}, "probes": {}, "inferences": []}
    with patch("custom_components.kiosk_satellite_manager.AdbClient") as client_cls, patch(
        "custom_components.kiosk_satellite_manager.CapabilityReportCollector.collect",
        new=AsyncMock(return_value=report),
    ):
        client_cls.return_value.connect = AsyncMock()
        client_cls.return_value.close = AsyncMock()
        response = await hass.services.async_call(
            DOMAIN, "capability_report", {"config_entry_id": ctx.entry.entry_id},
            blocking=True, return_response=True,
        )
    assert response == {"report": report}
    assert hass.services.supports_response(DOMAIN, "capability_report") is SupportsResponse.ONLY
    client_cls.return_value.close.assert_awaited_once()


async def test_capability_report_rejects_unknown_entry_and_closes_after_failure(hass):
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})):
        ctx = await init_integration(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "capability_report", {"config_entry_id": "not-an-entry"},
            blocking=True, return_response=True,
        )
    with patch("custom_components.kiosk_satellite_manager.AdbClient") as client_cls, patch(
        "custom_components.kiosk_satellite_manager.CapabilityReportCollector.collect",
        new=AsyncMock(side_effect=RuntimeError("probe failed")),
    ):
        client_cls.return_value.connect = AsyncMock()
        client_cls.return_value.close = AsyncMock()
        with pytest.raises(RuntimeError, match="probe failed"):
            await hass.services.async_call(
                DOMAIN, "capability_report", {"config_entry_id": ctx.entry.entry_id},
                blocking=True, return_response=True,
            )
    client_cls.return_value.close.assert_awaited_once()


async def test_onboarding_plan_returns_report_and_dry_run_plan(hass):
    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})):
        ctx = await init_integration(hass)
    report = {"schema_version": 1, "facts": {}, "probes": {}, "inferences": []}
    plan = {"schema_version": 1, "steps": [], "automatic_actions": [], "destructive_options": []}
    with patch("custom_components.kiosk_satellite_manager.AdbClient") as client_cls, patch(
        "custom_components.kiosk_satellite_manager.CapabilityReportCollector.collect",
        new=AsyncMock(return_value=report),
    ), patch("custom_components.kiosk_satellite_manager.build_onboarding_plan", return_value=plan):
        client_cls.return_value.connect = AsyncMock()
        client_cls.return_value.close = AsyncMock()
        response = await hass.services.async_call(
            DOMAIN, "onboarding_plan", {"config_entry_id": ctx.entry.entry_id},
            blocking=True, return_response=True,
        )
    assert response == {"report": report, "plan": plan}
    assert hass.services.supports_response(DOMAIN, "onboarding_plan") is SupportsResponse.ONLY
    client_cls.return_value.close.assert_awaited_once()
