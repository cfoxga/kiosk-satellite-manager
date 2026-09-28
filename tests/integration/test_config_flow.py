"""Config flow integration test (KSM-BEHAVE-001, extended by
KSM-BEHAVE-009/012). Drives the real ConfigFlow through phacc's flow
manager, with AdbClient mocked at the connect/getprop/shell boundary. Real
ADB I/O is covered live, not in this suite -- see docs/SPEC/provisioning.md.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from homeassistant import config_entries, data_entry_flow
from homeassistant.components import persistent_notification
from homeassistant.setup import async_setup_component

from custom_components.kiosk_satellite_manager.adb_client import (
    AdbAuthPending,
    AdbConnectFailed,
)
from custom_components.kiosk_satellite_manager.const import (
    CONF_AREA_ID,
    CONF_DEVICE_PROFILE,
    CONF_ENABLE_DEVICE_OWNER,
    CONF_EXISTING_INSTALL_ACTION,
    CONF_HA_TOKEN,
    CONF_HOME_LAUNCHER,
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_TOKEN_MODE,
    DOMAIN,
    EXISTING_INSTALL_REINSTALL,
    EXISTING_INSTALL_REUSE,
    TOKEN_MODE_AUTO,
)
from custom_components.kiosk_satellite_manager.config_flow import KioskSatelliteManagerConfigFlow
from custom_components.kiosk_satellite_manager.credentials import TokenCredential


def _getprop(**props: str) -> AsyncMock:
    """Prop-name-driven getprop mock.

    The device catalog (issue #20) reads a fixed allowlist of identity
    properties, and it grew from four to eleven when models and install
    recipes were split apart. Keying on the property name instead of call
    order keeps these flow tests pinned to *what* was read rather than to the
    order the collector happens to read it in. Anything not named here reads
    empty -- which is missing evidence, and never satisfies a match rule.
    """
    async def _read(name: str) -> str:
        return props.get(name, "")

    return AsyncMock(side_effect=_read)


async def test_selected_fleet_invitation_attempted_once_before_entry_creation(hass):
    """[KSM-TEST-247] A flow retry cannot send a second invitation."""
    flow = KioskSatelliteManagerConfigFlow()
    flow.hass = hass
    flow._host = "192.168.99.50"
    flow._discovered_name = "New Display"
    flow._selected_fleet_id = "fleet-ha"
    flow._existing_install_action = EXISTING_INSTALL_REUSE
    events = []

    async def invite(_hass, selected, host, password, pin):
        events.append(("invite", selected, host, password, pin))

    with patch("custom_components.kiosk_satellite_manager.config_flow._async_invite_to_fleet",
               new=invite), patch.object(flow, "_async_create_device_entry", side_effect=lambda: events.append(("create",))):
        await flow.async_step_install_done()
        await flow.async_step_install_done()
    assert events == [("invite", "fleet-ha", "192.168.99.50", None, None), ("create",), ("create",)]


async def test_failed_onboarding_invitation_is_reported_without_claiming_join(hass):
    """[KSM-TEST-247] A failed leader call leaves a recoverable entry and a notice."""
    flow = KioskSatelliteManagerConfigFlow()
    flow.hass = hass
    flow._host = "192.168.99.50"
    flow._selected_fleet_id = "fleet-ha"
    flow._existing_install_action = EXISTING_INSTALL_REUSE
    with patch("custom_components.kiosk_satellite_manager.config_flow._async_invite_to_fleet",
               new=AsyncMock(side_effect=ValueError("unreachable"))) as invite, patch(
        "custom_components.kiosk_satellite_manager.config_flow.persistent_notification.async_create"
    ) as notice, patch.object(flow, "_async_create_device_entry", return_value={"type": "create_entry"}):
        assert (await flow.async_step_install_done())["type"] == "create_entry"
        assert (await flow.async_step_install_done())["type"] == "create_entry"
    invite.assert_awaited_once()
    assert notice.call_count == 1
    assert "Unmanaged" in notice.call_args.kwargs["message"]


async def test_failed_fresh_install_cannot_invite_a_device(hass):
    """[KSM-TEST-247] Install failure leaves no authenticated KS target to invite."""
    flow = KioskSatelliteManagerConfigFlow()
    flow.hass = hass
    flow._host = "192.168.99.50"
    flow._selected_fleet_id = "fleet-ha"
    with patch("custom_components.kiosk_satellite_manager.config_flow._async_invite_to_fleet",
               new=AsyncMock()) as invite, patch.object(
        flow, "_async_create_device_entry", return_value={"type": "create_entry"}
    ):
        assert (await flow.async_step_install_done())["type"] == "create_entry"
    invite.assert_not_awaited()


async def test_selected_leader_lost_before_target_setup_stops_onboarding(hass):
    """[KSM-TEST-247] A stale selection cannot mutate the target device."""
    flow = KioskSatelliteManagerConfigFlow()
    flow.hass = hass
    flow._selected_fleet_id = "missing-fleet"
    flow._host = "192.168.99.50"
    with patch.object(flow, "async_abort", return_value={"type": "abort", "reason": "fleet_unavailable"}):
        assert (await flow.async_step_install())["reason"] == "fleet_unavailable"
    rejected = await flow.async_step_ks_device_info({"password": "synthetic-password"})
    assert rejected["errors"]["base"] == "fleet_unavailable"


_GTV_PROPS = {
    "ro.build.characteristics": "tv,nosdcard",
    "ro.product.manufacturer": "onn",
    "ro.product.model": "Google TV",
    "ro.build.version.sdk": "35",
    "ro.build.version.release": "15",
}
_PORTAL_GO_PROPS = {
    "ro.build.characteristics": "nosdcard",
    "ro.product.manufacturer": "Facebook",
    "ro.product.model": "PortalGo",
    "ro.product.device": "terry",
    "ro.build.version.sdk": "29",
}


async def test_abandoned_flow_revokes_only_auto_created_token(hass):
    """[KSM-TEST-102] Flow cancellation cleans up an unpersisted KSM token."""
    flow = KioskSatelliteManagerConfigFlow()
    flow.hass = hass
    flow._credential = TokenCredential("owned-access", "owned-refresh", owned=True)
    refresh_token = object()
    with patch.object(hass.auth, "async_get_refresh_token", return_value=refresh_token), patch.object(
        hass.auth, "async_remove_refresh_token", new=MagicMock()
    ) as remove_token:
        flow.async_abort(reason="user")
        await hass.async_block_till_done()

    remove_token.assert_called_once_with(refresh_token)

async def test_user_flow_shows_device_info_step_with_discovered_name_default(hass):
    """[KSM-TEST-049] Detected metadata precedes only KSM-owned fields."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_GTV_PROPS)
        mock_client.shell = AsyncMock(return_value="Living Room TV")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "device_info"
    schema_pass = next(k for k in result["data_schema"].schema if k == CONF_PASSWORD)
    # KSM-TEST-006: no hardcoded default password any more.
    assert schema_pass.default is vol.UNDEFINED
    assert CONF_NAME not in {field.schema for field in result["data_schema"].schema}
    assert CONF_AREA_ID not in {field.schema for field in result["data_schema"].schema}
    # KSM-BEHAVE-048 (issue #20): "onn" is a fallback *classification*, not an
    # exact catalog model -- the label says so, and the entry carries no model
    # key, so the Install button will refuse rather than run the Portal recipe
    # on unidentified hardware.
    assert result["description_placeholders"] == {
        "android_version": "Android 15 (SDK 35)",
        "device_model": "Google TV stick (onn/Chromecast-class, unrecognized model)",
    }


async def test_user_flow_lists_auto_create_then_existing_long_lived_tokens(hass):
    """[KSM-TEST-050] The picker never exposes a token secret in the form."""
    selected_token = SimpleNamespace(
        id="existing-token-id",
        client_name="Kitchen kiosk",
        token_type="long_lived_access_token",
    )
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch.object(
        hass.auth._store, "async_get_refresh_tokens", return_value=[selected_token]  # noqa: SLF001
    ):
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_GTV_PROPS)
        mock_client.shell = AsyncMock(return_value="Living Room TV")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )

    token_field = next(key for key in result["data_schema"].schema if key == CONF_TOKEN_MODE)
    options = result["data_schema"].schema[token_field].config["options"]
    assert [(option["value"], option["label"]) for option in options] == [
        (TOKEN_MODE_AUTO, "<Auto-create new token>"),
        ("existing-token-id", "Kitchen kiosk"),
    ]
    assert CONF_HA_TOKEN not in {field.schema for field in result["data_schema"].schema}


async def test_user_flow_identifies_portal_models_and_defaults_password(hass):
    """[KSM-TEST-003, KSM-TEST-005] User flow identifies Meta Portal models."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_PORTAL_GO_PROPS)
        mock_client.shell = AsyncMock(return_value="Test Portal Portal")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.40.224", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "device_info"
    assert result["description_placeholders"]["device_model"] == "Meta Portal Go"
    assert CONF_HOME_LAUNCHER not in {
        field.schema for field in result["data_schema"].schema
    }
    schema_pass = next(k for k in result["data_schema"].schema if k == CONF_PASSWORD)
    assert schema_pass.default is vol.UNDEFINED
    assert result["description_placeholders"]["android_version"] == "SDK 29"


async def test_user_flow_uses_and_normalizes_portal_bluetooth_name(hass):
    """[KSM-TEST-016] Portal labels live in bluetooth_name, not device_name."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_PORTAL_GO_PROPS)
        mock_client.shell = AsyncMock(return_value="Test Portal Portal")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.40.224", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert CONF_NAME not in {field.schema for field in result["data_schema"].schema}
    mock_client.shell.assert_awaited_once_with("settings get secure bluetooth_name")



async def test_user_flow_creates_entry_after_device_info_step(hass):
    # Successful CREATE_ENTRY triggers the real async_setup_entry, whose
    # coordinator does a first refresh -- fetch_health must be mocked for
    # that too, or phacc's pytest-socket blocks the real network call.
    # install_and_launch is mocked too (KSM-BEHAVE-012): the flow now runs
    # it itself as a progress step right after device_info.
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "unknown"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        # A real asyncio.sleep(0) forces the install task to genuinely
        # suspend at least once -- a bare AsyncMock() with no real await
        # inside resolves within the same eager-task step that creates it
        # (HA's async_create_task uses eager_start), so the flow would jump
        # straight past SHOW_PROGRESS to SHOW_PROGRESS_DONE before this test
        # ever gets a chance to observe the progress step, unlike a real
        # install, which always crosses real await points (network I/O).
        async def _install_ok(*args, **kwargs):
            await asyncio.sleep(0)

        mock_install.side_effect = _install_ok
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_GTV_PROPS)
        mock_client.shell = AsyncMock(return_value="Living Room TV")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "device_info"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "hunter222"},
        )
        # No install progress step: this device matches no exact catalog
        # model, so the install task fails closed at require_recipe before it
        # ever reaches a real await, and the flow goes straight to the entry.
        # A device that *does* resolve a recipe still shows progress -- see
        # test_user_flow_creates_entry_even_when_install_fails.
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        await hass.async_block_till_done()
        mock_install.assert_not_awaited()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room TV"
    assert result["data"][CONF_HOST] == "192.168.50.64"
    # KSM-TEST-060: the entry is created (pairing is the hard, human part and
    # must not be thrown away), but with no exact model key -- an onn/Google TV
    # stick has never had its ro.product.model recorded, so it matches only the
    # gtv_stick fallback classification. install_and_launch fails closed on
    # that, instead of silently applying the Meta Portal recipe.
    assert result["data"][CONF_DEVICE_PROFILE] is None
    assert result["data"][CONF_NAME] == "Living Room TV"
    assert result["data"][CONF_AREA_ID] is None
    assert result["data"][CONF_PASSWORD] == "hunter222"
    # Everything the user typed is preserved on the entry, and nothing was
    # pushed to the device: install_and_launch was never reached for a model
    # the catalog cannot resolve (asserted inside the patch block above).


async def test_user_flow_creates_entry_and_notifies_when_install_fails(hass):
    """[KSM-TEST-067] Failure stays best-effort but is visible in HA."""
    # KSM-BEHAVE-012: install is best-effort -- the device is already
    # paired by this point (the hard, human-in-the-loop part), so a failed
    # install shouldn't cost the user the whole flow. The button remains
    # the recovery path.
    assert await async_setup_component(hass, "persistent_notification", {})
    await hass.async_block_till_done()

    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "unknown"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        async def _install_fail(*args, **kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("network unreachable")

        mock_install.side_effect = _install_fail
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        # Portal Go, not a GTV stick: this test is about a *failing* install,
        # which means the flow has to reach install_and_launch at all. An
        # unmatched model fails closed before that (KSM-BEHAVE-048) and is
        # covered separately.
        mock_client.getprop = _getprop(**_PORTAL_GO_PROPS)
        mock_client.shell = AsyncMock(return_value="null")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.77", "port": 5555}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "hunter222"},
        )
        assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS

        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_NAME] == "192.168.50.77"
    # The flow's own inputs reach install_and_launch unchanged, including the
    # resolved model key the catalog gate matched on.
    mock_install.assert_awaited_once()
    _, kwargs = mock_install.await_args
    assert kwargs["host"] == "192.168.50.77"
    assert kwargs["password"] == "hunter222"
    assert kwargs["device_model"] == "portal_go"
    notification = persistent_notification._async_get_or_create_notifications(hass).get(  # noqa: SLF001
        "kiosk_satellite_manager_install_failed_192.168.50.77"
    )
    assert notification is not None
    assert "192.168.50.77" in notification["message"]
    assert "Install/Reinstall" in notification["message"]


async def test_user_flow_device_info_uses_host_for_provisioning_when_device_name_unset(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_GTV_PROPS)
        mock_client.shell = AsyncMock(return_value="null")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.77", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "device_info"
    assert CONF_NAME not in {field.schema for field in result["data_schema"].schema}


async def test_user_flow_shows_auth_pending_error_when_device_never_confirms(hass):
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.config_flow.CONNECT_RETRY_DELAY_S", 0
    ):
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock(side_effect=AdbAuthPending("never tapped"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.99", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "auth_pending"


async def test_user_flow_keeps_unreadable_identity_and_release_conservative(hass):
    """[KSM-TEST-079] Missing probes are evidence-free, not a guessed recipe."""
    async def _unreadable_props(name: str) -> str:
        if name in {"ro.product.model", "ro.build.version.release"}:
            raise RuntimeError(f"cannot read {name}")
        return ""

    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = AsyncMock(side_effect=_unreadable_props)
        mock_client.shell = AsyncMock(return_value="Unidentified kiosk")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.100", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "device_info"
    assert result["description_placeholders"] == {
        "android_version": "Unknown",
        "device_model": "Unknown device",
    }
    mock_client.close.assert_awaited_once()


async def test_user_flow_shows_cannot_connect_for_immediate_refusal(hass):
    """[KSM-TEST-080] Connection refusal stays distinct from auth pending."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock(side_effect=AdbConnectFailed("refused"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.101", "port": 5555}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "cannot_connect"
    mock_client.connect.assert_awaited_once()


@pytest.mark.parametrize(
    ("selected_token_id", "resolved_token"),
    [
        ("deleted-token-id", None),
        (
            "wrong-type-token-id",
            SimpleNamespace(
                id="wrong-type-token-id",
                client_name="Wrong type",
                token_type="normal",
            ),
        ),
    ],
)
async def test_user_flow_rejects_missing_or_non_long_lived_selected_token(
    hass, selected_token_id, resolved_token
):
    """[KSM-TEST-081] Invalid selector values neither mint nor install."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch.object(
        hass.auth._store, "async_get_refresh_tokens", return_value=[]  # noqa: SLF001
    ), patch.object(
        hass.auth, "async_get_refresh_token", return_value=resolved_token
    ) as mock_get_token, patch.object(
        hass.auth, "async_create_access_token"
    ) as mock_create_token, patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_PORTAL_GO_PROPS)
        mock_client.shell = AsyncMock(return_value="Office Display")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.102", "port": 5555}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "admin", CONF_TOKEN_MODE: selected_token_id},
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "device_info"
    assert result["errors"][CONF_TOKEN_MODE] == "token_not_found"
    mock_get_token.assert_called_once_with(selected_token_id)
    mock_create_token.assert_not_called()
    mock_install.assert_not_awaited()


async def test_user_flow_uses_selected_long_lived_token(hass):
    """[KSM-TEST-050] Selected token IDs resolve only at submission."""
    async def _install_ok(*args, **kwargs):
        await asyncio.sleep(0)
        return kwargs["token_credential"]

    selected_token = SimpleNamespace(
        id="existing-token-id",
        client_name="Kitchen kiosk",
        token_type="long_lived_access_token",
    )

    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch.object(
        hass.auth._store, "async_get_refresh_tokens", return_value=[selected_token]  # noqa: SLF001
    ), patch.object(
        hass.auth, "async_get_refresh_token", return_value=selected_token
    ), patch.object(
        hass.auth, "async_create_access_token", return_value="selected-access-token"
    ), patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "unknown"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch",
        side_effect=_install_ok,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        # Portal Go: the token this test selects is only consumed once
        # install_and_launch actually runs, which needs an approved recipe.
        mock_client.getprop = _getprop(**_PORTAL_GO_PROPS)
        mock_client.shell = AsyncMock(return_value="Office Display")
        mock_client.is_ks_installed = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.88", "port": 5555}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_PASSWORD: "admin",
                CONF_TOKEN_MODE: "existing-token-id",
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN_MODE] == "existing-token-id"
    assert result["data"][CONF_HA_TOKEN] == "selected-access-token"
    assert result["data"][CONF_HOME_LAUNCHER] is False
    assert not persistent_notification._async_get_or_create_notifications(hass)  # noqa: SLF001


async def test_user_flow_kept_install_collects_only_existing_connection_details(hass):
    """[KSM-TEST-021] A kept install needs only KSM entry details and the
    existing remote-UI password; it must not overwrite device settings."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.62"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_GTV_PROPS)
        mock_client.shell = AsyncMock(return_value="Living Room TV")
        mock_client.is_ks_installed = AsyncMock(return_value=True)
        mock_client.uninstall_ks = AsyncMock()
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        # KSM-BEHAVE-021: the package choice comes *before* device_info.
        assert result["step_id"] == "existing_install"
        action_key = next(
            k for k in result["data_schema"].schema if k == CONF_EXISTING_INSTALL_ACTION
        )
        assert action_key.default() == EXISTING_INSTALL_REUSE

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EXISTING_INSTALL_ACTION: EXISTING_INSTALL_REUSE}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "existing_device_info"
        schema_fields = {field.schema for field in result["data_schema"].schema}
        # KSM-BEHAVE-098 (#62): the Device Owner opt-in is the one addition.
        assert schema_fields == {CONF_PASSWORD, CONF_ENABLE_DEVICE_OWNER}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "hunter222"},
        )
        assert result["type"] in (
            data_entry_flow.FlowResultType.CREATE_ENTRY,
            data_entry_flow.FlowResultType.SHOW_PROGRESS,
        )
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            assert result["step_id"] == "install"
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
            await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room TV"
    assert CONF_HOME_LAUNCHER not in result["data"]
    assert CONF_HA_TOKEN not in result["data"]
    mock_install.assert_not_awaited()
    mock_client.uninstall_ks.assert_not_awaited()


async def test_user_flow_reinstall_uninstalls_before_install(hass):
    """[KSM-TEST-022] Reinstall keeps the full fresh-install form (including
    token selection) and uninstalls before installing.

    Uses Portal Go props deliberately: since KSM-BEHAVE-048 the reinstall path
    resolves an approved recipe *before* it uninstalls, so an unmatched device
    never reaches the uninstall at all (asserted separately below)."""
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.62"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        call_order = []

        async def _uninstall(*args, **kwargs):
            call_order.append("uninstall")

        async def _install_ok(*args, **kwargs):
            call_order.append("install")
            await asyncio.sleep(0)

        mock_install.side_effect = _install_ok
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_PORTAL_GO_PROPS)
        mock_client.shell = AsyncMock(return_value="Living Room TV")
        mock_client.is_ks_installed = AsyncMock(return_value=True)
        mock_client.uninstall_ks = AsyncMock(side_effect=_uninstall)
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "existing_install"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EXISTING_INSTALL_ACTION: EXISTING_INSTALL_REINSTALL},
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "device_info"
        schema_fields = {field.schema for field in result["data_schema"].schema}
        assert CONF_HOME_LAUNCHER not in schema_fields
        assert CONF_TOKEN_MODE in schema_fields
        assert CONF_HA_TOKEN not in schema_fields

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "hunter222"},
        )
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
            await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    mock_client.uninstall_ks.assert_awaited_once()
    mock_install.assert_awaited_once()
    assert call_order == ["uninstall", "install"]


async def test_user_flow_reinstall_never_uninstalls_a_device_with_no_approved_recipe(hass):
    """[KSM-TEST-060] Fail closed *before* the destructive half of reinstall.

    `install_and_launch` raises `NoApprovedRecipe` for an unmatched model, but
    the config flow uninstalls the existing app first. If the gate ran only
    inside `install_and_launch`, this GTV stick would end the flow with its
    working Kiosk Satellite removed and no recipe able to put it back -- a
    fail-closed check that fails *after* the damage. The entry is still
    created (pairing is the hard, human part), so the Install button remains
    the recovery path once #18 records an exact model for this hardware.
    """
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.62"}),
    ), patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock()
        mock_client.getprop = _getprop(**_GTV_PROPS)
        mock_client.shell = AsyncMock(return_value="Living Room TV")
        mock_client.is_ks_installed = AsyncMock(return_value=True)
        mock_client.uninstall_ks = AsyncMock()
        mock_client.close = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.50.64", "port": 5555}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EXISTING_INSTALL_ACTION: EXISTING_INSTALL_REINSTALL},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PASSWORD: "hunter222"},
        )
        if result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
            await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_PROFILE] is None
    mock_client.uninstall_ks.assert_not_awaited()
    mock_install.assert_not_awaited()


_PIN = "ab" * 32
_PORTAL_MINI_HEALTH = {
    "appVersion": "2026.9.84",
    "brand": "Facebook",
    "model": "Facebook PortalMini",
    "androidVersion": "Android 10",
    "sdkInt": 29,
    "name": "Great Room Portal",
}


async def _start_ks_flow(hass, host="192.168.40.250"):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: host, "port": 5555}
    )


async def test_ks_running_device_is_added_without_adb(hass, ks_health_probe, tls_migration):
    """[KSM-TEST-185] KSM-BEHAVE-096: a host whose Kiosk Satellite answers
    health is added from health + password alone -- no AdbClient at all."""
    ks_health_probe.return_value = (None, _PORTAL_MINI_HEALTH)
    tls_migration.return_value = _PIN
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.config_flow.login",
        new=AsyncMock(return_value="tok"),
    ) as mock_login, patch(
        "custom_components.kiosk_satellite_manager.config_flow.install_and_launch"
    ) as mock_install, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.84"}),
    ):
        result = await _start_ks_flow(hass)
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "ks_device_info"
        assert {field.schema for field in result["data_schema"].schema} == {CONF_PASSWORD}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "hunter222"}
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Great Room Portal"
    data = result["data"]
    assert data[CONF_HOST] == "192.168.40.250"
    assert data[CONF_DEVICE_PROFILE] == "portal_mini"
    assert data[CONF_PASSWORD] == "hunter222"
    assert data["tls_spki_sha256"] == _PIN
    assert data["port"] == 5555 and data["key_path"]
    assert CONF_HOME_LAUNCHER not in data and CONF_HA_TOKEN not in data
    mock_client_cls.assert_not_called()
    mock_install.assert_not_called()
    tls_migration.assert_awaited_once()
    assert tls_migration.await_args.args[1:] == ("192.168.40.250", "hunter222")
    # The password is checked over the pinned channel the TLS step returned.
    assert mock_login.await_args.kwargs["pin"] == _PIN
    assert mock_login.await_args.args[1:] == ("192.168.40.250", "hunter222")


async def test_host_without_kiosk_satellite_still_uses_adb(hass, ks_health_probe):
    """[KSM-TEST-185] negative: health does not answer -> the ADB connect
    path runs exactly as before."""
    ks_health_probe.return_value = None
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.connect = AsyncMock(side_effect=AdbConnectFailed("refused"))
        mock_client.close = AsyncMock()
        result = await _start_ks_flow(hass, "192.168.50.99")

    ks_health_probe.assert_awaited_once()
    mock_client.connect.assert_awaited()
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_ks_device_info_rejects_wrong_password_and_tls_failure(
    hass, ks_health_probe, tls_migration
):
    """[KSM-TEST-186] KSM-BEHAVE-096: a rejected login or a failed TLS
    switch re-shows the form with an error and creates no entry."""
    from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

    ks_health_probe.return_value = (_PIN, _PORTAL_MINI_HEALTH)
    tls_migration.return_value = _PIN
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.AdbClient"
    ) as mock_client_cls, patch(
        "custom_components.kiosk_satellite_manager.config_flow.login",
        new=AsyncMock(side_effect=KsApiError("invalid password")),
    ):
        result = await _start_ks_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "wrong"}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "ks_device_info"
        assert result["errors"] == {"base": "invalid_auth"}

        tls_migration.side_effect = KsApiError("did not answer over HTTPS")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "hunter222"}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect_ks"}

    mock_client_cls.assert_not_called()
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    ("login_effects", "tls_pin", "expected_error", "tls_awaited"),
    [
        # HTTP device, wrong password: reported as such, TLS never switched.
        (["bad"], None, "invalid_auth", False),
        (["down"], None, "cannot_connect_ks", False),
        (["tok", "down"], "ab" * 32, "cannot_connect_ks", True),
    ],
)
async def test_ks_device_info_http_device_error_paths(
    hass, ks_health_probe, tls_migration, login_effects, tls_pin, expected_error, tls_awaited
):
    """[KSM-TEST-186] KSM-BEHAVE-096 step 4 on an HTTP-only device (the
    Master Bedroom Portal's case): the password is checked before the TLS
    switch, so a wrong one is `invalid_auth`, not a TLS failure."""
    import aiohttp

    from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

    effects = {
        "bad": KsApiError("invalid password"),
        "down": aiohttp.ClientConnectionError("refused"),
        "tok": "tok",
    }
    ks_health_probe.return_value = (None, _PORTAL_MINI_HEALTH)
    tls_migration.return_value = tls_pin
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.login",
        new=AsyncMock(side_effect=[effects[e] for e in login_effects]),
    ):
        result = await _start_ks_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "hunter222"}
        )

    assert result["errors"] == {"base": expected_error}
    assert tls_migration.await_count == (1 if tls_awaited else 0)
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_ks_device_on_pre_tls_release_is_added_unpinned(
    hass, ks_health_probe, tls_migration
):
    """[KSM-TEST-185] a Kiosk Satellite too old for HTTPS (establish returns
    None) is added on HTTP with no pin after one HTTP login; unknown models
    get no profile (fails closed)."""
    ks_health_probe.return_value = (
        None,
        {"appVersion": "2026.9.50", "brand": "onn", "model": "Mystery Box", "name": ""},
    )
    tls_migration.return_value = None
    with patch(
        "custom_components.kiosk_satellite_manager.config_flow.login",
        new=AsyncMock(return_value="tok"),
    ) as mock_login, patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.50"}),
    ):
        result = await _start_ks_flow(hass, "10.0.0.77")
        assert result["description_placeholders"]["android_version"] == "Unknown"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "hunter222"}
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "10.0.0.77"
    assert "tls_spki_sha256" not in result["data"]
    assert result["data"][CONF_DEVICE_PROFILE] is None
    assert mock_login.await_count == 1
    assert mock_login.await_args.kwargs["pin"] is None
