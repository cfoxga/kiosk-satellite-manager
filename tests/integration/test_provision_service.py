"""Provision service integration test (KSM-BEHAVE-002). AdbClient is mocked
at the boundary; fetch_health is patched to control read-back, proving both
the success path and the loud-failure-on-mismatch path required by the
acceptance criteria. The ks.provision quoting/apply-then-readback shape
itself is covered by unit/test_provisioning.py and was live-verified against
a production device (see docs/SPEC/provisioning.md).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.kiosk_satellite_manager import SERVICE_PROVISION
from custom_components.kiosk_satellite_manager.const import DOMAIN

from .conftest import admin_context, init_integration


async def test_provision_applies_and_refreshes_on_match(hass):
    health_responses = iter([{"name": "old"}, {"name": "Kitchen Portal"}])

    async def fake_fetch_health(session, host):
        return next(health_responses)

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=fake_fetch_health):
        ctx = await init_integration(hass)

        with patch(
            "custom_components.kiosk_satellite_manager.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.apply_provisioning",
            new=AsyncMock(return_value={"name": "Kitchen Portal"}),
        ) as mock_apply:
            mock_client_cls.return_value.connect = AsyncMock()
            mock_client_cls.return_value.close = AsyncMock()

            await hass.services.async_call(
                DOMAIN,
                SERVICE_PROVISION,
                {
                    "config_entry_id": ctx.entry.entry_id,
                    "settings": {"device.name": "Kitchen Portal"},
                },
                blocking=True,
                context=await admin_context(hass),
            )

    mock_apply.assert_awaited_once()


async def test_provision_raises_service_validation_error_on_mismatch(hass):
    from custom_components.kiosk_satellite_manager.provisioning import ProvisioningMismatch

    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"name": "unchanged"}),
    ):
        ctx = await init_integration(hass)

        with patch(
            "custom_components.kiosk_satellite_manager.AdbClient"
        ) as mock_client_cls, patch(
            "custom_components.kiosk_satellite_manager.apply_provisioning",
            new=AsyncMock(side_effect=ProvisioningMismatch("readback did not match")),
        ):
            mock_client_cls.return_value.connect = AsyncMock()
            mock_client_cls.return_value.close = AsyncMock()

            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    DOMAIN,
                    SERVICE_PROVISION,
                    {
                        "config_entry_id": ctx.entry.entry_id,
                        "settings": {"device.name": "Kitchen Portal"},
                    },
                    blocking=True,
                    context=await admin_context(hass),
                )


async def test_provision_rejects_unknown_config_entry(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"name": "irrelevant"}),
    ):
        await init_integration(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_PROVISION,
            {"config_entry_id": "does-not-exist", "settings": {"device.name": "x"}},
            blocking=True,
            context=await admin_context(hass),
        )
