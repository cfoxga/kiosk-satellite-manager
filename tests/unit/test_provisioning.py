"""Unit tests for the provisioning payload applier (KSM-BEHAVE-083, #47).

*Superseded 2026-09-24*: provisioning now applies over one
`PATCH /api/settings` (`ks_api_client.patch_settings`) instead of the
`ks.provision` ADB intent -- see provisioning.py's module docstring. These
tests pin the apply-then-readback shape: patch_settings runs first (and
already raises on a per-key rejection), then /api/health is read back for
whatever keys it echoes.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError
from custom_components.kiosk_satellite_manager.provisioning import (
    ProvisioningMismatch,
    apply_provisioning,
)

_PATCH_SETTINGS = "custom_components.kiosk_satellite_manager.provisioning.ks_api_client.patch_settings"


def _fake_session(health: dict):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=health)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


async def test_apply_provisioning_succeeds_when_readback_matches():
    session = _fake_session({"name": "Kitchen Portal"})
    with patch(_PATCH_SETTINGS, new=AsyncMock(return_value={"rejected": []})) as patch_settings:
        result = await apply_provisioning(
            session, "1.2.3.4", "tok-789", {"device.name": "Kitchen Portal"}, pin=None,
        )
    assert result == {"name": "Kitchen Portal"}
    patch_settings.assert_awaited_once_with(
        session, "1.2.3.4", "tok-789", {"device.name": "Kitchen Portal"}, pin=None
    )


async def test_apply_provisioning_raises_on_readback_mismatch():
    session = _fake_session({"name": "Theater Google TV"})
    with patch(_PATCH_SETTINGS, new=AsyncMock(return_value={"rejected": []})):
        with pytest.raises(ProvisioningMismatch):
            await apply_provisioning(session, "1.2.3.4", "tok-789", {"device.name": "Kitchen Portal"}, pin=None)


async def test_apply_provisioning_ignores_non_verifiable_keys():
    session = _fake_session({"name": "unchanged"})
    with patch(_PATCH_SETTINGS, new=AsyncMock(return_value={"rejected": []})):
        result = await apply_provisioning(session, "1.2.3.4", "tok-789", {"remote.password": "x"}, pin=None)
    assert result == {"name": "unchanged"}


async def test_apply_provisioning_never_reads_back_on_a_rejected_key():
    """[KSM-TEST-159] negative case: a rejected key raises before any
    /api/health readback, since patch_settings itself already raises."""
    session = _fake_session({"name": "unchanged"})
    with patch(
        _PATCH_SETTINGS, new=AsyncMock(side_effect=KsApiError("device rejected settings: ['device.name']"))
    ):
        with pytest.raises(KsApiError, match="device.name"):
            await apply_provisioning(session, "1.2.3.4", "tok-789", {"device.name": "Kitchen Portal"}, pin=None)
    session.get.assert_not_called()
