"""Adding an uncovered Portal hostname to HA's Let's Encrypt add-on
(KSM-BEHAVE-182, #178)."""
from __future__ import annotations

import copy
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.kiosk_satellite_manager import le_addon, le_certificate

HOST = "test-portal-gen2.cfoxga.com"
SECRET = "cf-token-must-never-appear"
MATERIAL = le_certificate.CertificateMaterial("cert", "key", "cd" * 32, "ef" * 32)


def _options(*domains):
    return {
        "domains": list(domains), "email": "ops@example.com", "challenge": "dns",
        "dns": {"provider": "dns-cloudflare", "cloudflare_api_token": SECRET},
        "keyfile": "privkey.pem", "certfile": "fullchain.pem",
    }


class FakeAddons:
    """Supervisor add-on client double: records writes, plays back states."""

    def __init__(self, options, states=("started", "stopped"), *, missing=False):
        self.options = options
        self.states = list(states)
        self.state = "stopped"
        self.missing = missing
        self.writes: list[dict] = []
        self.starts = 0

    async def addon_info(self, slug):
        assert slug == "core_letsencrypt"
        if self.missing:
            raise RuntimeError("Addon core_letsencrypt is not installed")
        if self.starts and self.states:
            self.state = self.states.pop(0)
        return SimpleNamespace(options=copy.deepcopy(self.options), state=self.state)

    async def set_addon_options(self, slug, options):
        self.writes.append(copy.deepcopy(options.config))
        self.options = copy.deepcopy(options.config)

    async def start_addon(self, slug):
        self.starts += 1
        self.state = "started"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(le_addon, "POLL_S", 0)


def _run(hass, addons, loads):
    calls = iter(loads)

    def load(hostname):
        assert hostname == HOST
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return (
        patch.object(le_addon, "_addons", return_value=addons),
        patch.object(le_certificate, "load_for_hostname", side_effect=load),
    )


async def test_uncovered_hostname_is_appended_and_issued(hass, caplog):
    """[KSM-TEST-361] Every other option (the DNS credential included) is
    written back unchanged, the add-on runs to completion, and the covering
    certificate is returned."""
    caplog.set_level(logging.DEBUG)
    addons = FakeAddons(_options("ha-dev.cfoxga.com"), states=("startup", "started", "stopped"))
    a, b = _run(hass, addons, [MATERIAL])
    with a, b:
        result = await le_addon.async_add_hostname(hass, HOST)
    assert result == MATERIAL
    assert addons.writes == [{**_options("ha-dev.cfoxga.com", HOST)}]
    assert addons.starts == 1
    assert addons.states == []  # polled until the run stopped
    assert SECRET not in caplog.text


async def test_listed_hostname_is_not_duplicated(hass):
    """[KSM-TEST-361] Already in `domains` but not on the certificate: run only."""
    addons = FakeAddons(_options("ha-dev.cfoxga.com", HOST))
    a, b = _run(hass, addons, [MATERIAL])
    with a, b:
        assert await le_addon.async_add_hostname(hass, HOST) == MATERIAL
    assert addons.writes == []
    assert addons.starts == 1


@pytest.mark.parametrize("states,loads", [
    (("started", "error"), [MATERIAL]),
    (("started", "stopped"), [le_certificate.HostnameNotCovered("still not covered")]),
    (("started",) * 50, [MATERIAL]),
], ids=["add-on error", "still uncovered", "timeout"])
async def test_failed_run_restores_previous_domains(hass, monkeypatch, caplog, states, loads):
    """[KSM-TEST-361] Negative: no covering certificate -> the previous options
    are written back, so one unissuable name can't break every renewal."""
    monkeypatch.setattr(le_addon, "RUN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(le_addon, "POLL_S", 0.01)
    addons = FakeAddons(_options("ha-dev.cfoxga.com"), states=states)
    a, b = _run(hass, addons, loads)
    with a, b, pytest.raises(le_certificate.CertificateUnavailable) as err:
        await le_addon.async_add_hostname(hass, HOST)
    assert addons.writes == [_options("ha-dev.cfoxga.com", HOST), _options("ha-dev.cfoxga.com")]
    assert addons.options == _options("ha-dev.cfoxga.com")
    assert "log" in str(err.value)
    assert SECRET not in str(err.value) and SECRET not in caplog.text


async def test_failed_run_of_listed_name_writes_nothing(hass):
    """[KSM-TEST-361] Negative: nothing was appended, so nothing is rolled back."""
    addons = FakeAddons(_options(HOST), states=("started", "error"))
    a, b = _run(hass, addons, [MATERIAL])
    with a, b, pytest.raises(le_certificate.CertificateUnavailable):
        await le_addon.async_add_hostname(hass, HOST)
    assert addons.writes == []


async def test_no_supervisor_or_addon_writes_nothing(hass):
    """[KSM-TEST-361] Negative: no Supervisor (not HA OS/Supervised) or no
    add-on installed -> fail with the manual step, no writes, no run."""
    with pytest.raises(le_certificate.CertificateUnavailable) as err:
        await le_addon.async_add_hostname(hass, HOST)  # test hass has no hassio
    assert HOST in str(err.value)

    addons = FakeAddons(_options(), missing=True)
    with patch.object(le_addon, "_addons", return_value=addons), pytest.raises(
        le_certificate.CertificateUnavailable
    ) as err:
        await le_addon.async_add_hostname(hass, HOST)
    assert addons.writes == [] and addons.starts == 0
    assert HOST in str(err.value)
