"""Package verifier wiring (KSM-BEHAVE-184, #179).

The device-side reads and writes are unit-tested in test_install.py; these
prove the Install and Uninstall buttons and onboarding persist and restore
the prior value.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from custom_components.kiosk_satellite_manager.button import async_install_entry
from custom_components.kiosk_satellite_manager.const import (
    CONF_AREA_ID,
    CONF_DEVICE_PROFILE,
    CONF_HOST,
    CONF_PACKAGE_VERIFIER_PRIOR,
    CONF_PASSWORD,
    CONF_PRIVATE_DNS_PRIOR,
    DOMAIN,
)
from custom_components.kiosk_satellite_manager.ks_update import ApiInstallUnavailable

from .conftest import init_integration
from .test_config_flow import _PORTAL_GO_PROPS, _getprop

_BUTTON = "custom_components.kiosk_satellite_manager.button."
_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"


def _health():
    return patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.62"}))


async def test_KSM_TEST_368_install_press_records_the_prior_verifier_value(hass):
    async def _install(*args, **kwargs):
        kwargs["on_package_verifier_disabled"]("1")

    with _health(), patch(_BUTTON + "AdbClient") as cls, patch(
        _BUTTON + "install_and_launch", new=AsyncMock(side_effect=_install)
    ), patch(_BUTTON + "async_get_clientsession"), patch(
        _BUTTON + "async_self_update_entry",
        new=AsyncMock(side_effect=ApiInstallUnavailable("API offline")),
    ):
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_go"})
        cls.return_value.connect = AsyncMock()
        cls.return_value.close = AsyncMock()
        await async_install_entry(hass, ctx.entry)
    assert ctx.entry.data[CONF_PACKAGE_VERIFIER_PRIOR] == "1"


async def _press_uninstall(hass, entry, restored=True):
    with patch(_BUTTON + "AdbClient") as cls, patch(
        _BUTTON + "restore_package_verifier", new=AsyncMock(return_value=restored)
    ) as restore, patch(_BUTTON + "restore_private_dns", new=AsyncMock(return_value=True)):
        client = cls.return_value
        client.connect = AsyncMock()
        client.uninstall_ks = AsyncMock()
        client.close = AsyncMock()
        await hass.services.async_call(
            "button", "press",
            {"entity_id": er.async_get(hass).async_get_entity_id(
                "button", DOMAIN, f"{entry.entry_id}_uninstall"
            )},
            blocking=True,
        )
    return client, restore


async def test_KSM_TEST_368_uninstall_restores_the_recorded_value_and_forgets_it(hass):
    with _health():
        ctx = await init_integration(hass, data={CONF_PACKAGE_VERIFIER_PRIOR: "1"})
        client, restore = await _press_uninstall(hass, ctx.entry)
    client.uninstall_ks.assert_awaited_once()
    restore.assert_awaited_once_with(client, "1")
    assert CONF_PACKAGE_VERIFIER_PRIOR not in ctx.entry.data


async def test_KSM_TEST_368_uninstall_forgets_both_restored_priors(hass):
    """Both restores update the entry in turn; the second must not bring the
    first key back."""
    with _health():
        ctx = await init_integration(
            hass, data={CONF_PACKAGE_VERIFIER_PRIOR: "1", CONF_PRIVATE_DNS_PRIOR: "opportunistic"}
        )
        await _press_uninstall(hass, ctx.entry)
    assert CONF_PACKAGE_VERIFIER_PRIOR not in ctx.entry.data
    assert CONF_PRIVATE_DNS_PRIOR not in ctx.entry.data


async def test_KSM_TEST_368_uninstall_keeps_the_record_when_it_did_not_restore(hass):
    with _health():
        ctx = await init_integration(hass, data={CONF_PACKAGE_VERIFIER_PRIOR: ""})
        _client, restore = await _press_uninstall(hass, ctx.entry, restored=False)
    restore.assert_awaited_once()
    assert ctx.entry.data[CONF_PACKAGE_VERIFIER_PRIOR] == ""


async def test_KSM_TEST_368_uninstall_without_a_record_never_touches_the_verifier(hass):
    with _health():
        ctx = await init_integration(hass)
        client, restore = await _press_uninstall(hass, ctx.entry)
    client.uninstall_ks.assert_awaited_once()
    restore.assert_not_awaited()


async def test_KSM_TEST_368_onboarding_stores_the_prior_verifier_value(hass):
    ar.async_get(hass).async_create("KSM Test Area")
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, _health(), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        async def _install(*args, **kwargs):
            kwargs["on_package_verifier_disabled"]("")
            await asyncio.sleep(0)

        mock_install.side_effect = _install
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_PORTAL_GO_PROPS)
        mock_client.shell = AsyncMock(return_value="Kitchen Portal")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.40.133", "port": 5555}
        )
        assert result["step_id"] == "device_info"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AREA_ID: "ksm_test_area", CONF_PASSWORD: "hunter222"}
        )
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
            await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PACKAGE_VERIFIER_PRIOR] == ""
