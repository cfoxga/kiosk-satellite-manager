"""KSM-BEHAVE-096: the unauthenticated Kiosk Satellite health probe that
decides whether Add Device can skip ADB ([KSM-TEST-185] probe half)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp

from custom_components.kiosk_satellite_manager import config_flow

_HEALTH = {"appVersion": "2026.9.84", "model": "Acme Widget"}
_API = "custom_components.kiosk_satellite_manager.ks_api_client"


async def _probe(https, http):
    with patch(f"{_API}.probe_https", new=AsyncMock(return_value=https)), patch(
        f"{_API}.get_health", new=http
    ):
        return await config_flow._async_probe_ks_health(object(), "10.0.0.5")


async def test_https_answer_returns_served_pin_and_health():
    http = AsyncMock()
    assert await _probe(("ab" * 32, _HEALTH), http) == ("ab" * 32, _HEALTH)
    http.assert_not_awaited()


async def test_http_answer_returns_no_pin():
    assert await _probe(None, AsyncMock(return_value=_HEALTH)) == (None, _HEALTH)


async def test_no_kiosk_satellite_returns_none():
    refused = AsyncMock(side_effect=aiohttp.ClientConnectionError("refused"))
    assert await _probe(None, refused) is None
    assert await _probe(None, AsyncMock(side_effect=TimeoutError())) is None
    # Something answers on 2324 but is not Kiosk Satellite.
    assert await _probe(None, AsyncMock(return_value={"status": "ok"})) is None
    assert await _probe(("ab" * 32, ["not", "a", "dict"]), AsyncMock()) is None
