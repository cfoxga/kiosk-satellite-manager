"""KS follows HA-side renames without a user present (KSM-BEHAVE-102,
#64, ham#184; KSM-TEST-194)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError

from custom_components.kiosk_satellite_manager import SERVICE_RENAME_DEVICE
from custom_components.kiosk_satellite_manager.const import CONF_HOST, DOMAIN, MANAGER_ENTRY_KEY

from .conftest import init_integration

_FETCH_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_LOGIN = "custom_components.kiosk_satellite_manager.ks_api_login"
_APPLY_KS = "custom_components.kiosk_satellite_manager.apply_rename_ks_settings"
RENAME_KEY = "kiosk_satellite_manager_rename"


async def test_ksm_test_194_trusted_rename_callable(hass):
    """[KSM-TEST-194] The in-process callable renames with no user context and
    returns the service's shape; manager/unknown targets are rejected before
    device I/O; the public service stays fail-closed."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "old"})):
        ctx = await init_integration(hass, data={CONF_HOST: "192.168.99.99"})
        rename = hass.data[RENAME_KEY]

        with patch(_LOGIN, new=AsyncMock(return_value="device-token")) as login, patch(
            _APPLY_KS, new=AsyncMock(return_value="applied")
        ):
            result = await rename(ctx.entry.entry_id, "Master Bedroom Kiosk")
            assert result["ks"] == "applied"
            assert result["entry"] == "applied"
            assert ctx.entry.title == "Master Bedroom Kiosk"
            assert login.await_count == 1

            manager_id = hass.data[MANAGER_ENTRY_KEY]
            for target in (manager_id, "missing-entry"):
                with pytest.raises(ServiceValidationError):
                    await rename(target, "Den Kiosk")
            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    DOMAIN, SERVICE_RENAME_DEVICE,
                    {"config_entry_id": ctx.entry.entry_id, "name": "Den Kiosk"},
                    blocking=True, return_response=True, context=Context(),
                )
            assert login.await_count == 1
    assert ctx.entry.title == "Master Bedroom Kiosk"
