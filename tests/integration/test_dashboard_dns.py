"""Private DNS and dashboard DNS wiring (KSM-BEHAVE-152/153, #121).

The device-side reads and writes are unit-tested in test_install.py and
test_adb_client.py; these prove the button, config flow and repair issue
persist and surface them.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries, data_entry_flow
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.kiosk_satellite_manager.button import async_install_entry
from custom_components.kiosk_satellite_manager.const import (
    CONF_DEVICE_PROFILE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PRIVATE_DNS_PRIOR,
    DOMAIN,
)
from custom_components.kiosk_satellite_manager.device_repairs import apply_dashboard_dns
from custom_components.kiosk_satellite_manager.install import (
    DashboardDnsCheck,
    probe_dashboard_dns,
)

from .conftest import init_integration
from .test_config_flow import _PORTAL_GO_PROPS, _getprop

_BUTTON = "custom_components.kiosk_satellite_manager.button."
_CHECK = "custom_components.kiosk_satellite_manager.install.check_dashboard_dns"
_MISMATCH = DashboardDnsCheck("ha.cfoxga.com", "99.1.33.71", frozenset({"192.168.40.115"}))
_MATCH = DashboardDnsCheck("ha.cfoxga.com", "192.168.40.115", frozenset({"192.168.40.115"}))


@pytest.fixture(autouse=True)
def _no_recheck_delay():
    with patch("custom_components.kiosk_satellite_manager.install._DNS_RECHECK_SECONDS", 0):
        yield


async def test_KSM_TEST_306_mismatch_raises_and_a_match_clears_a_repair(hass):
    """KSM-BEHAVE-154: keyed by the device's entry or subentry ID, not its host."""
    issue_id = "dashboard_dns_dev-a"
    apply_dashboard_dns(hass, "dev-a", _MISMATCH, "Kitchen Portal", "192.168.40.133")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.translation_key == "dashboard_dns_mismatch"
    assert issue.translation_placeholders == {
        "name": "Kitchen Portal",
        "host": "192.168.40.133",
        "hostname": "ha.cfoxga.com",
        "device_address": "99.1.33.71",
        "ha_addresses": "192.168.40.115",
    }
    assert ir.async_get(hass).async_get_issue(DOMAIN, "dashboard_dns_192.168.40.133") is None

    # A new address for the same device updates its one repair.
    apply_dashboard_dns(hass, "dev-a", _MISMATCH, "Kitchen Portal", "192.168.40.134")
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, issue_id
    ).translation_placeholders["host"] == "192.168.40.134"

    apply_dashboard_dns(hass, "dev-a", _MATCH, "Kitchen Portal", "192.168.40.134")
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_KSM_TEST_306_a_mismatch_is_rechecked_before_it_is_reported(hass):
    """Android re-reads its resolver config asynchronously after the Private
    DNS change, so the first answer can still come from the old resolver."""
    with patch(_CHECK, new=AsyncMock(side_effect=[_MISMATCH, _MATCH])) as check:
        result = await probe_dashboard_dns(hass, object(), "192.168.40.133", "https://ha.cfoxga.com")
    assert check.await_count == 2
    assert result is _MATCH

    with patch(_CHECK, new=AsyncMock(side_effect=[_MISMATCH, _MISMATCH])) as check:
        result = await probe_dashboard_dns(hass, object(), "192.168.40.133", "https://ha.cfoxga.com")
    assert check.await_count == 2
    assert result is _MISMATCH


async def test_KSM_TEST_306_dashboard_dns_check_never_fails_an_install(hass):
    with patch(_CHECK, new=AsyncMock(side_effect=OSError("resolver down"))):
        assert await probe_dashboard_dns(
            hass, object(), "192.168.40.133", "https://ha.cfoxga.com"
        ) is None


async def test_KSM_TEST_310_install_press_keys_the_repair_by_device_id(hass):
    async def _install(*args, **kwargs):
        kwargs["on_dashboard_dns"](_MISMATCH)

    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.62"})), patch(
        _BUTTON + "AdbClient"
    ) as cls, patch(
        _BUTTON + "install_and_launch", new=AsyncMock(side_effect=_install)
    ), patch(_BUTTON + "async_get_clientsession"):
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_go"})
        cls.return_value.connect = AsyncMock()
        cls.return_value.close = AsyncMock()
        await async_install_entry(hass, ctx.entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"dashboard_dns_{ctx.entry.entry_id}")
    assert issue is not None
    assert issue.translation_placeholders["host"] == ctx.entry.data[CONF_HOST]
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"dashboard_dns_{ctx.entry.data[CONF_HOST]}"
    ) is None


async def test_KSM_TEST_305_install_press_records_the_prior_private_dns_mode(hass):
    async def _install(*args, **kwargs):
        kwargs["on_private_dns_disabled"]("opportunistic")

    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.62"})), patch(
        _BUTTON + "AdbClient"
    ) as cls, patch(
        _BUTTON + "install_and_launch", new=AsyncMock(side_effect=_install)
    ), patch(_BUTTON + "async_get_clientsession"):
        ctx = await init_integration(hass, data={CONF_DEVICE_PROFILE: "portal_go"})
        cls.return_value.connect = AsyncMock()
        cls.return_value.close = AsyncMock()
        await async_install_entry(hass, ctx.entry)
    assert ctx.entry.data[CONF_PRIVATE_DNS_PRIOR] == "opportunistic"


async def _press_uninstall(hass, entry, restored=True):
    with patch(_BUTTON + "AdbClient") as cls, patch(
        _BUTTON + "restore_private_dns", new=AsyncMock(return_value=restored)
    ) as restore:
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


async def test_KSM_TEST_305_uninstall_restores_the_recorded_mode_and_forgets_it(hass):
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.62"})):
        ctx = await init_integration(hass, data={CONF_PRIVATE_DNS_PRIOR: ""})
        client, restore = await _press_uninstall(hass, ctx.entry)
    client.uninstall_ks.assert_awaited_once()
    restore.assert_awaited_once_with(client, "")
    assert CONF_PRIVATE_DNS_PRIOR not in ctx.entry.data


async def test_KSM_TEST_305_uninstall_keeps_the_record_when_it_did_not_restore(hass):
    """A mode that did not read back off (the user's newer choice, or an
    unreadable setting) is left alone, and so is the record of the prior."""
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.62"})):
        ctx = await init_integration(hass, data={CONF_PRIVATE_DNS_PRIOR: "opportunistic"})
        _client, restore = await _press_uninstall(hass, ctx.entry, restored=False)
    restore.assert_awaited_once()
    assert ctx.entry.data[CONF_PRIVATE_DNS_PRIOR] == "opportunistic"


async def test_KSM_TEST_305_uninstall_without_a_record_never_touches_private_dns(hass):
    with patch("custom_components.kiosk_satellite_manager.fetch_health",
               new=AsyncMock(return_value={"appVersion": "2026.9.62"})):
        ctx = await init_integration(hass)
        client, restore = await _press_uninstall(hass, ctx.entry)
    client.uninstall_ks.assert_awaited_once()
    restore.assert_not_awaited()


async def test_KSM_TEST_305_onboarding_stores_the_prior_private_dns_mode_and_dns_repair(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.62"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        async def _install(*args, **kwargs):
            kwargs["on_private_dns_disabled"]("")
            kwargs["on_dashboard_dns"](_MISMATCH)
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
            result["flow_id"], {CONF_PASSWORD: "hunter222"}
        )
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
            await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PRIVATE_DNS_PRIOR] == ""
    # [KSM-TEST-310] the flow has no entry ID yet; the created entry's first
    # setup raises onboarding's mismatch under its ID.
    issues = ir.async_get(hass)
    issue = issues.async_get_issue(DOMAIN, f"dashboard_dns_{result['result'].entry_id}")
    assert issue is not None
    assert issue.translation_placeholders["host"] == "192.168.40.133"
    assert issues.async_get_issue(DOMAIN, "dashboard_dns_192.168.40.133") is None
