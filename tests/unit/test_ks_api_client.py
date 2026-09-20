"""Unit tests for the Kiosk Satellite on-device web UI client
(KSM-BEHAVE-010/011). Endpoint shapes were extracted live from the Test
Portal's own served JS bundles -- see ks_api_client.py's module docstring.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.kiosk_satellite_manager import ks_api_client
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError


def _fake_response(json_data: dict, ok: bool = True, status: int = 200):
    resp = MagicMock()
    resp.ok = ok
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.raise_for_status = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_session(**method_returns):
    session = MagicMock()
    for method, return_value in method_returns.items():
        setattr(session, method, MagicMock(return_value=return_value))
    return session


async def test_get_setup_status_gets_unauthenticated_status_url():
    resp_cm = _fake_response({"setupNeeded": True, "passwordNeeded": True})
    session = _fake_session(get=resp_cm)
    result = await ks_api_client.get_setup_status(session, "192.168.1.50")
    assert result == {"setupNeeded": True, "passwordNeeded": True}
    session.get.assert_called_once()
    assert session.get.call_args.args[0] == "http://192.168.1.50:2324/api/setup/status"


async def test_setup_password_posts_password_and_device_name_returns_token():
    session = _fake_session(post=_fake_response({"token": "tok-123"}))
    token = await ks_api_client.setup_password(session, "192.168.1.50", "hunter22", "Kitchen")
    assert token == "tok-123"
    _, kwargs = session.post.call_args
    assert kwargs["json"] == {"password": "hunter22", "deviceName": "Kitchen"}


async def test_setup_password_raises_ksapierror_on_rejection():
    session = _fake_session(post=_fake_response({"error": "already set"}, ok=False, status=403))
    with pytest.raises(KsApiError, match="already set"):
        await ks_api_client.setup_password(session, "192.168.1.50", "hunter22", "Kitchen")


async def test_login_posts_password_returns_token():
    session = _fake_session(post=_fake_response({"token": "tok-456"}))
    token = await ks_api_client.login(session, "192.168.1.50", "hunter22")
    assert token == "tok-456"
    _, kwargs = session.post.call_args
    assert kwargs["json"] == {"password": "hunter22"}


async def test_patch_settings_sends_bearer_token_and_values():
    session = _fake_session(patch=_fake_response({"rejected": []}))
    await ks_api_client.patch_settings(
        session, "192.168.1.50", "tok-789", {"device.name": "Kitchen"}
    )
    _, kwargs = session.patch.call_args
    assert kwargs["json"] == {"device.name": "Kitchen"}
    assert kwargs["headers"]["Authorization"] == "Bearer tok-789"


async def test_patch_settings_raises_when_key_rejected():
    session = _fake_session(patch=_fake_response({"rejected": ["ha.url"]}))
    with pytest.raises(KsApiError, match="ha.url"):
        await ks_api_client.patch_settings(
            session, "192.168.1.50", "tok-789", {"ha.url": "not a url"}
        )


async def test_check_ha_connection_returns_ok_flag():
    session = _fake_session(post=_fake_response({"ok": True}))
    assert await ks_api_client.check_ha_connection(session, "192.168.1.50", "tok-789") is True


async def test_check_ha_connection_false_on_failure():
    session = _fake_session(post=_fake_response({"ok": False, "error": "unreachable"}))
    assert await ks_api_client.check_ha_connection(session, "192.168.1.50", "tok-789") is False
