"""Unit tests for the provisioning payload builder/applier (KSM-BEHAVE-002).

build_provision_command's quoting and the apply-then-readback sequence were
both live-verified this session against a production Kiosk Satellite device
(docs/SPEC/provisioning.md) -- these tests pin that exact shape so a future
edit can't regress it silently.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.kiosk_satellite_manager.provisioning import (
    ProvisioningMismatch,
    apply_provisioning,
    build_provision_command,
)


def test_build_provision_command_shape():
    cmd = build_provision_command({"device.name": "Kitchen Portal"})
    assert cmd == (
        "am start -a android.intent.action.MAIN -n me.jxl.kiosk_satellite/.MainActivity "
        "--es ks.provision '{\"device.name\": \"Kitchen Portal\"}'"
    )


def test_build_provision_command_escapes_embedded_single_quotes():
    cmd = build_provision_command({"device.name": "Chris's Portal"})
    assert "'\\''" in cmd


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
    client = MagicMock()
    client.shell = AsyncMock(return_value="")
    session = _fake_session({"name": "Kitchen Portal"})
    result = await apply_provisioning(client, session, "1.2.3.4", {"device.name": "Kitchen Portal"})
    assert result == {"name": "Kitchen Portal"}
    client.shell.assert_awaited_once()


async def test_apply_provisioning_raises_on_readback_mismatch():
    client = MagicMock()
    client.shell = AsyncMock(return_value="")
    session = _fake_session({"name": "Theater Google TV"})
    with pytest.raises(ProvisioningMismatch):
        await apply_provisioning(client, session, "1.2.3.4", {"device.name": "Kitchen Portal"})


async def test_apply_provisioning_ignores_non_verifiable_keys():
    client = MagicMock()
    client.shell = AsyncMock(return_value="")
    session = _fake_session({"name": "unchanged"})
    result = await apply_provisioning(client, session, "1.2.3.4", {"remote.password": "x"})
    assert result == {"name": "unchanged"}
