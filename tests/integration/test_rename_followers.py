"""KS follows HA-side renames without a user present (KSM-BEHAVE-099/102,
#64, ham#184; KSM-TEST-191/194)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.kiosk_satellite_manager import SERVICE_RENAME_DEVICE
from custom_components.kiosk_satellite_manager.const import CONF_HOST, DOMAIN, MANAGER_ENTRY_KEY
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

from .conftest import init_integration

_FETCH_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
_LOGIN = "custom_components.kiosk_satellite_manager.ks_api_login"
_APPLY_KS = "custom_components.kiosk_satellite_manager.apply_rename_ks_settings"
_FOLLOW_LOGIN = "custom_components.kiosk_satellite_manager.rename.ks_login"
_FOLLOW_GET = "custom_components.kiosk_satellite_manager.rename.get_settings"
_FOLLOW_PATCH = "custom_components.kiosk_satellite_manager.rename.patch_settings"
RENAME_KEY = "kiosk_satellite_manager_rename"


async def _rename_entity(hass, domain: str, object_id: str, new_entity_id: str) -> None:
    ent_reg = er.async_get(hass)
    old = ent_reg.async_get_or_create(domain, "test", object_id, suggested_object_id=object_id).entity_id
    ent_reg.async_update_entity(old, new_entity_id=new_entity_id)
    await hass.async_block_till_done()


async def test_ksm_test_191_satellite_entity_follows_rename(hass):
    """[KSM-TEST-191] Only the device whose ha.satellite_entity equals the old
    ID is patched; a failing device does not block the others; a non
    assist_satellite rename makes no device contact."""
    with patch(_FETCH_HEALTH, new=AsyncMock(return_value={"name": "x"})):
        await init_integration(hass, data={CONF_HOST: "10.0.0.1"})
        await init_integration(hass, data={CONF_HOST: "10.0.0.2"})
        await init_integration(hass, data={CONF_HOST: "10.0.0.3"})

    settings = {
        "10.0.0.1": {"ha.satellite_entity": "assist_satellite.great_room_kiosk"},
        "10.0.0.2": {"ha.satellite_entity": "assist_satellite.kitchen"},
    }

    async def fake_login(session, host, password, *, pin):
        if host == "10.0.0.3":
            raise KsApiError("offline")
        return f"token-{host}"

    async def fake_get(session, host, token, *, pin):
        return settings[host]

    with patch(_FOLLOW_LOGIN, new=AsyncMock(side_effect=fake_login)) as login, patch(
        _FOLLOW_GET, new=AsyncMock(side_effect=fake_get)
    ), patch(_FOLLOW_PATCH, new=AsyncMock(return_value={})) as patch_settings:
        await _rename_entity(hass, "sensor", "great_room_kiosk", "sensor.master_bedroom_kiosk")
        login.assert_not_awaited()

        await _rename_entity(
            hass, "assist_satellite", "great_room_kiosk", "assist_satellite.master_bedroom_kiosk"
        )

    assert {call.args[1] for call in login.await_args_list} == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}
    patch_settings.assert_awaited_once()
    args, kwargs = patch_settings.await_args
    assert args[1] == "10.0.0.1"
    assert args[3] == {"ha.satellite_entity": "assist_satellite.master_bedroom_kiosk"}


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
