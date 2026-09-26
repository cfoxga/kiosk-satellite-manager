"""KSM-TEST-077: service targets must still be active KSM entries."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import (
    SERVICE_CAPABILITY_REPORT,
    SERVICE_ONBOARDING_PLAN,
    SERVICE_PROVISION,
    SERVICE_RENAME_DEVICE,
)
from custom_components.kiosk_satellite_manager.const import DOMAIN

from .conftest import init_integration


@pytest.mark.parametrize(
    ("service", "payload", "return_response"),
    [
        (SERVICE_PROVISION, {"settings": {"device.name": "x"}}, False),
        (SERVICE_CAPABILITY_REPORT, {}, True),
        (SERVICE_ONBOARDING_PLAN, {}, True),
        (SERVICE_RENAME_DEVICE, {"name": "Allowed device"}, True),
    ],
)
async def test_services_reject_entry_without_active_coordinator_before_adb(
    hass, service, payload, return_response
):
    """A stale config entry is not a valid target after its coordinator is gone."""
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})
    ):
        ctx = await init_integration(hass)
    hass.data[DOMAIN].pop(ctx.entry.entry_id)

    with patch("custom_components.kiosk_satellite_manager.AdbClient") as client_cls:
        with pytest.raises(ServiceValidationError, match="not active"):
            await hass.services.async_call(
                DOMAIN,
                service,
                {"config_entry_id": ctx.entry.entry_id, **payload},
                blocking=True,
                return_response=return_response,
            )

    client_cls.assert_not_called()


@pytest.mark.parametrize("target_kind", ["unknown", "wrong_domain"])
@pytest.mark.parametrize(
    ("service", "payload", "return_response"),
    [
        (SERVICE_PROVISION, {"settings": {"device.name": "x"}}, False),
        (SERVICE_CAPABILITY_REPORT, {}, True),
        (SERVICE_ONBOARDING_PLAN, {}, True),
        (SERVICE_RENAME_DEVICE, {"name": "Allowed device"}, True),
    ],
)
async def test_services_reject_unknown_or_wrong_domain_targets_before_adb(
    hass, target_kind, service, payload, return_response
):
    """Service validation rejects every non-KSM target before any ADB work."""
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})
    ):
        await init_integration(hass)
    if target_kind == "unknown":
        target_id = "does-not-exist"
    else:
        wrong_domain_entry = MockConfigEntry(domain="light", data={})
        wrong_domain_entry.add_to_hass(hass)
        target_id = wrong_domain_entry.entry_id

    with patch("custom_components.kiosk_satellite_manager.AdbClient") as client_cls:
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                service,
                {"config_entry_id": target_id, **payload},
                blocking=True,
                return_response=return_response,
            )

    client_cls.assert_not_called()
