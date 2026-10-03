"""KSM-TEST-189/190 (kiosk-satellite-manager#62): the add-device flow's
opt-in Enable Device Owner, driven through HA's real ConfigFlow with the real
device_owner module against the scripted Portal shell from #54."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.helpers import area_registry as ar

from custom_components.kiosk_satellite_manager import device_owner
from custom_components.kiosk_satellite_manager.adb_client import AdbConnectFailed
from custom_components.kiosk_satellite_manager.const import (
    CONF_AREA_ID,
    CONF_DEVICE_PROFILE,
    CONF_ENABLE_DEVICE_OWNER,
    CONF_HOST,
    CONF_PASSWORD,
    DOMAIN,
)
from ksm_device_owner_fake import FakeDevice, KS_ADMIN, SECRET

HOST = "192.0.2.61"
_MINI_PROPS = {
    "ro.build.characteristics": "nosdcard",
    "ro.product.manufacturer": "Facebook",
    "ro.product.model": "PortalMini",
    "ro.build.version.sdk": "29",
}
_NOTIFY = "custom_components.kiosk_satellite_manager.config_flow.persistent_notification.async_create"


@pytest.fixture(autouse=True)
def _area_for_onboarding(hass):
    ar.async_get(hass).async_create("KSM Test Area")


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(device_owner, "_POLL_INTERVAL_S", 0)


@pytest.fixture(autouse=True)
def meta_start():
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.meta_setup.async_start",
        new=AsyncMock(),
    ) as start:
        yield start


def _client_class(fake: FakeDevice, *, owner_connect_error: Exception | None = None):
    """config_flow's AdbClient: detection (connect 1) and install (connect 2)
    succeed; every later connect -- the Device Owner steps -- raises
    `owner_connect_error` when given."""
    connects: list[int] = []

    class Client:
        def __init__(self, host, port, key_path):
            pass

        async def connect(self, auth_timeout_s: float = 5):
            connects.append(1)
            if owner_connect_error and len(connects) > 2:
                raise owner_connect_error

        async def close(self):
            pass

        async def getprop(self, name):
            return _MINI_PROPS.get(name, "")

        async def is_ks_installed(self):
            return False

        async def shell(self, command):
            if command == "settings get secure bluetooth_name":
                return "Great Room"
            return await fake.shell(command)

    return Client


async def _add(hass, fake, *, opt_in, owner_connect_error=None):
    """Drive the add flow through install; return (result, notify mock)."""

    async def _install_ok(*args, **kwargs):
        await asyncio.sleep(0)

    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient",
        _client_class(fake, owner_connect_error=owner_connect_error),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch",
        side_effect=_install_ok,
    ), patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "unknown"}),
    ), patch(_NOTIFY) as notify:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST, "port": 5555}
        )
        assert result["step_id"] == "device_info"
        assert CONF_ENABLE_DEVICE_OWNER in {k.schema for k in result["data_schema"].schema}
        default = next(
            k for k in result["data_schema"].schema if k == CONF_ENABLE_DEVICE_OWNER
        ).default()
        assert default is False
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AREA_ID: "ksm_test_area", CONF_PASSWORD: "pw", CONF_ENABLE_DEVICE_OWNER: opt_in}
        )
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        await hass.async_block_till_done()
    return result, notify


async def _add_and_confirm(hass, fake, confirm: bool):
    async def _install_ok(*args, **kwargs):
        await asyncio.sleep(0)

    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient",
        _client_class(fake),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch",
        side_effect=_install_ok,
    ), patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "unknown"}),
    ), patch(_NOTIFY) as notify:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST, "port": 5555}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AREA_ID: "ksm_test_area", CONF_PASSWORD: "pw", CONF_ENABLE_DEVICE_OWNER: True}
        )
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "onboard_device_owner"
        summary = result["description_placeholders"]["accounts"]
        assert "com.facebook.aloha.sso" in summary
        assert SECRET not in str(result)
        assert fake.mutations() == []
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"confirm": confirm}
        )
        await hass.async_block_till_done()
    return result, notify


async def test_opt_in_confirmed_enrolls_and_creates_entry(hass, meta_start):
    """[KSM-TEST-189] Ticked + confirmed: one enrollment, entry created, and
    (KSM-TEST-217) Meta setup started, whose notice replaces 'enabled'."""
    fake = FakeDevice()
    result, notify = await _add_and_confirm(hass, fake, confirm=True)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_PROFILE] == "portal_mini"
    assert fake.mutations().count(f"dpm set-device-owner {KS_ADMIN}") == 1
    assert fake.owner == "me.jxl.kiosk_satellite" and fake.meta_installed
    meta_start.assert_awaited_once()
    target = meta_start.await_args.args[1]
    assert (target.host, target.password, target.model_key) == (HOST, "pw", "portal_mini")
    notify.assert_not_called()


async def test_opt_in_meta_setup_failure_is_reported(hass, meta_start):
    """[KSM-TEST-217] Negative: Meta setup not shown after enrollment -- the
    entry is still created and the notice says Device Owner is on and why
    the setup screen failed."""
    meta_start.side_effect = device_owner.DeviceOwnerError(
        "meta_setup_failed", "the setup screen did not come to the front"
    )
    fake = FakeDevice()
    result, notify = await _add_and_confirm(hass, fake, confirm=True)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    message = notify.call_args.kwargs["message"]
    assert "now Device Owner" in message and "did not come to the front" in message


async def test_opt_in_unconfirmed_skips_enrollment(hass):
    """[KSM-TEST-189] Negative: confirmation unticked -> no mutation, entry
    still created."""
    fake = FakeDevice()
    result, notify = await _add_and_confirm(hass, fake, confirm=False)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert fake.mutations() == []
    assert fake.owner is None
    notify.assert_not_called()


async def test_opt_out_never_contacts_device_for_owner(hass):
    """[KSM-TEST-189] Negative: toggle off -> no preflight at all."""
    fake = FakeDevice()
    result, notify = await _add(hass, fake, opt_in=False)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert fake.commands == []
    notify.assert_not_called()


@pytest.mark.parametrize("kwargs,text", [
    ({"owner": "me.jxl.kiosk_satellite"}, "already Device Owner"),
    ({"owner": "com.example.dpc"}, "com.example.dpc"),
    ({"account_types": ("com.example.other",)}, "cannot safely clear"),
    ({"users": 2}, "more than one Android user"),
])
async def test_opt_in_blocked_still_creates_entry(hass, kwargs, text):
    """[KSM-TEST-190] A preflight blocker never fails onboarding: entry
    created, no mutation, notification names the reason."""
    fake = FakeDevice(**kwargs)
    result, notify = await _add(hass, fake, opt_in=True)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert fake.mutations() == []
    notify.assert_called_once()
    assert text in notify.call_args.kwargs["message"]


async def test_opt_in_adb_unreachable_still_creates_entry(hass):
    """[KSM-TEST-190] ADB gone after install: entry created, notification
    names ADB."""
    fake = FakeDevice()
    result, notify = await _add(
        hass, fake, opt_in=True, owner_connect_error=AdbConnectFailed("refused")
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert fake.mutations() == []
    notify.assert_called_once()
    assert "ADB" in notify.call_args.kwargs["message"]


async def test_opt_in_enrollment_failure_still_creates_entry(hass):
    """[KSM-TEST-190] set-device-owner refused: entry created, package
    restored, notification names the failure."""
    fake = FakeDevice(set_owner_output="IllegalStateException", set_owner_takes_effect=False)
    result, notify = await _add_and_confirm(hass, fake, confirm=True)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert fake.meta_installed is True
    notify.assert_called_once()
    assert "refused" in notify.call_args.kwargs["message"]
    assert SECRET not in notify.call_args.kwargs["message"]


async def test_opt_in_on_kept_install_reaches_confirmation(hass):
    """[KSM-TEST-189] The keep-existing-install form carries the same opt-in:
    ticked, the flow reaches the confirmation form instead of the entry."""
    from custom_components.kiosk_satellite_manager.const import (
        CONF_EXISTING_INSTALL_ACTION,
        EXISTING_INSTALL_REUSE,
    )

    fake = FakeDevice()
    client_cls = _client_class(fake)

    async def _installed(self):
        return True

    client_cls.is_ks_installed = _installed
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient", client_cls
    ), patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "unknown"}),
    ), patch(_NOTIFY):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST, "port": 5555}
        )
        assert result["step_id"] == "existing_install"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EXISTING_INSTALL_ACTION: EXISTING_INSTALL_REUSE}
        )
        assert result["step_id"] == "existing_device_info"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AREA_ID: "ksm_test_area", CONF_PASSWORD: "pw", CONF_ENABLE_DEVICE_OWNER: True}
        )
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "onboard_device_owner"
        assert fake.mutations() == []
        await hass.config_entries.flow.async_configure(result["flow_id"], {"confirm": False})
        await hass.async_block_till_done()
