"""KSM-TEST-192/193 (#65): KSM creates or adopts each device's Voice Satellite
entry, binds KS's `ha.satellite_entity` to it, and -- only after the user
confirms a repair -- installs Voice Satellite through HACS. Real hass via
phacc; Voice Satellite is a mock integration whose config flow mirrors
upstream 2026.9.13 (one `name` field, unique ID = slug)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from custom_components.kiosk_satellite_manager.const import CONF_NAME, CONF_PASSWORD, DOMAIN
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

from .conftest import init_integration

VS = "voice_satellite"
REPO = "jxlarrea/voice-satellite-card-integration"
ISSUE = "voice_satellite_missing"
HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"
KS = "custom_components.kiosk_satellite_manager.ks_api_client"


class _VoiceSatelliteFlow(config_entries.ConfigFlow):
    """Upstream voice_satellite 2026.9.13 config flow, verbatim in shape."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            name = user_input["name"].strip()
            await self.async_set_unique_id(name.lower().replace(" ", "_"))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=name, data={"name": name})
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required("name"): str})
        )


async def _vs_setup_entry(hass, entry):
    er.async_get(hass).async_get_or_create(
        "assist_satellite", VS, entry.entry_id, config_entry=entry,
        suggested_object_id=entry.unique_id,
    )
    return True


def _install_voice_satellite(hass) -> None:
    mock_integration(
        hass,
        MockModule(VS, async_setup_entry=_vs_setup_entry, async_unload_entry=AsyncMock(return_value=True)),
        built_in=False,
    )
    mock_platform(hass, f"{VS}.config_flow", None)


def _satellite(hass, vs_entry) -> str:
    [ent] = [
        e for e in er.async_entries_for_config_entry(er.async_get(hass), vs_entry.entry_id)
        if e.domain == "assist_satellite"
    ]
    return ent.entity_id


def _fake_hacs(hass, *, repo: bool = True, download=None):
    repository = MagicMock()
    repository.data.installed = False
    repository.async_download_repository = AsyncMock(side_effect=download)
    hacs = MagicMock()
    hacs.repositories.get_by_full_name = MagicMock(return_value=repository if repo else None)
    hacs.data.async_write = AsyncMock()
    hacs.async_recreate_entities = AsyncMock()
    hass.data["hacs"] = hacs
    return hacs, repository


@pytest.fixture(autouse=True)
def vs_flow():
    with mock_config_flow(VS, _VoiceSatelliteFlow):
        yield


@pytest.fixture
def ks_api():
    login = AsyncMock(return_value="ks-token")
    patch_settings = AsyncMock(return_value={})
    get_settings = AsyncMock(return_value={})
    with patch(f"{KS}.login", new=login), patch(f"{KS}.patch_settings", new=patch_settings), patch(
        f"{KS}.get_settings", new=get_settings
    ), patch(HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.86"})):
        yield login, patch_settings, get_settings


async def _add_device(hass, name: str, host: str = "192.168.99.99", **data):
    ctx = await init_integration(hass, data={CONF_NAME: name, "host": host, **data})
    await hass.async_block_till_done(wait_background_tasks=True)
    return ctx.entry


def _bound(patch_settings) -> list[tuple[str, str]]:
    return [
        (call.args[1], call.args[3]["ha.satellite_entity"])
        for call in patch_settings.await_args_list
        if "ha.satellite_entity" in call.args[3]
    ]


async def test_setup_creates_satellite_entry_and_binds_ks(hass, ks_api):
    """[KSM-TEST-192] No Voice Satellite entry for the name: exactly one is
    created (unique ID = slug) and KS is pointed at its satellite entity."""
    _install_voice_satellite(hass)
    _, patch_settings, _ = ks_api
    await _add_device(hass, "Great Room Kiosk")

    [vs_entry] = hass.config_entries.async_entries(VS)
    assert vs_entry.unique_id == "great_room_kiosk"
    assert vs_entry.title == "Great Room Kiosk"
    satellite = _satellite(hass, vs_entry)
    assert satellite.startswith("assist_satellite.")
    assert _bound(patch_settings) == [("192.168.99.99", satellite)]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None


async def test_setup_adopts_existing_entry_by_name(hass, ks_api):
    """[KSM-TEST-192] A hand-made entry with the device's name is reused,
    never duplicated."""
    _install_voice_satellite(hass)
    manual = MockConfigEntry(
        domain=VS, unique_id="basement_office_kiosk", title="Basement Office Kiosk",
        data={"name": "Basement Office Kiosk"},
    )
    manual.add_to_hass(hass)
    assert await hass.config_entries.async_setup(manual.entry_id)
    _, patch_settings, _ = ks_api

    await _add_device(hass, "Basement Office Kiosk")

    assert hass.config_entries.async_entries(VS) == [manual]
    assert _bound(patch_settings) == [("192.168.99.99", _satellite(hass, manual))]


async def test_bind_failures_never_fail_the_device(hass, ks_api):
    """[KSM-TEST-192] Negative: a KS rejection, an unreadable binding and a
    missing password leave the device entry loaded; without a password KS is
    never contacted and no Voice Satellite entry is made."""
    _install_voice_satellite(hass)
    login, patch_settings, get_settings = ks_api
    patch_settings.side_effect = KsApiError("device rejected settings: ['ha.satellite_entity']")
    rejected = await _add_device(hass, "Theater GTV")
    assert rejected.state is ConfigEntryState.LOADED
    assert patch_settings.await_count == 1

    login.reset_mock()
    no_password = await _add_device(hass, "Test Kiosk", host="192.168.99.98", **{CONF_PASSWORD: None})
    assert no_password.state is ConfigEntryState.LOADED
    login.assert_not_awaited()
    assert {e.unique_id for e in hass.config_entries.async_entries(VS)} == {"theater_gtv"}

    get_settings.side_effect = KsApiError("HTTP 401")
    unreadable = await _add_device(hass, "Office Kiosk", host="192.168.99.97")
    assert unreadable.state is ConfigEntryState.LOADED
    assert patch_settings.await_count == 1
    assert "office_kiosk" not in {e.unique_id for e in hass.config_entries.async_entries(VS)}


async def test_live_binding_is_kept_even_under_another_name(hass, ks_api):
    """[KSM-TEST-192] A device already pointed at an existing Voice Satellite
    entity keeps it: no entry is created and KS is not patched. A binding
    whose entity is gone, or that names a non-Voice-Satellite entity, is
    replaced by name as usual."""
    _install_voice_satellite(hass)
    manual = MockConfigEntry(
        domain=VS, unique_id="theater_google_tv", title="Theater Google TV",
        data={"name": "Theater Google TV"},
    )
    manual.add_to_hass(hass)
    assert await hass.config_entries.async_setup(manual.entry_id)
    live = _satellite(hass, manual)
    _, patch_settings, get_settings = ks_api

    get_settings.return_value = {"ha.satellite_entity": live}
    await _add_device(hass, "Theater GTV")
    assert hass.config_entries.async_entries(VS) == [manual]
    assert _bound(patch_settings) == []

    other = er.async_get(hass).async_get_or_create("assist_satellite", "esphome", "abc")
    for stale in ("assist_satellite.gone", other.entity_id):
        get_settings.return_value = {"ha.satellite_entity": stale}
        await _add_device(hass, "Kitchen Kiosk", host="192.168.99.96")
        [kitchen] = [e for e in hass.config_entries.async_entries(VS) if e.unique_id == "kitchen_kiosk"]
        assert _bound(patch_settings)[-1] == ("192.168.99.96", _satellite(hass, kitchen))


async def test_absent_integration_asks_without_installing(hass, ks_api):
    """[KSM-TEST-193] Voice Satellite not installed: one fixable repair, and
    nothing is downloaded until the user confirms."""
    _, repository = _fake_hacs(hass)
    _, patch_settings, _ = ks_api
    await _add_device(hass, "Great Room Kiosk")
    await _add_device(hass, "Theater GTV", host="192.168.99.98")

    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)
    assert issue is not None and issue.is_fixable
    repository.async_download_repository.assert_not_awaited()
    assert _bound(patch_settings) == []


async def test_confirming_the_repair_installs_through_hacs_and_binds(hass, ks_api):
    """[KSM-TEST-193] Confirm: HACS downloads the repository by full name,
    persists its data, and every device is bound without a restart."""
    assert await async_setup_component(hass, "repairs", {})
    hacs, repository = _fake_hacs(hass, download=lambda **_: _install_voice_satellite(hass))
    _, patch_settings, _ = ks_api
    await _add_device(hass, "Great Room Kiosk")
    await _add_device(hass, "Theater GTV", host="192.168.99.98")
    assert hass.config_entries.async_entries(VS) == []

    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": ISSUE})
    assert result["type"] is FlowResultType.FORM
    repository.async_download_repository.assert_not_awaited()
    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    hacs.repositories.get_by_full_name.assert_called_with(REPO)
    repository.async_download_repository.assert_awaited_once()
    hacs.data.async_write.assert_awaited()
    assert {e.unique_id for e in hass.config_entries.async_entries(VS)} == {"great_room_kiosk", "theater_gtv"}
    assert sorted(host for host, _ in _bound(patch_settings)) == ["192.168.99.98", "192.168.99.99"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None


@pytest.mark.parametrize("setup,reason", [
    ("no_hacs", "hacs_missing"),
    ("unknown_repo", "repository_unknown"),
    ("download_error", "download_failed"),
    ("still_absent", "restart_required"),
])
async def test_repair_that_cannot_install_says_why(hass, ks_api, setup, reason):
    """[KSM-TEST-193] Negative: no HACS, an unknown repository, a download
    error, or an integration that still doesn't resolve abort with a reason
    and create no Voice Satellite entry."""
    assert await async_setup_component(hass, "repairs", {})
    repository = None
    if setup == "unknown_repo":
        _fake_hacs(hass, repo=False)
    elif setup == "download_error":
        _, repository = _fake_hacs(hass, download=RuntimeError("GitHub rate limited"))
    elif setup == "still_absent":
        _, repository = _fake_hacs(hass)
    await _add_device(hass, "Great Room Kiosk")

    manager = repairs_flow_manager(hass)
    result = await manager.async_init(DOMAIN, data={"issue_id": ISSUE})
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert hass.config_entries.async_entries(VS) == []
    if repository is not None:
        repository.async_download_repository.assert_awaited_once()


class _RenamedFieldFlow(_VoiceSatelliteFlow):
    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="x", data={"name": user_input["satellite_name"]})
        return await super().async_step_user(None)


class _AbortingFlow(_VoiceSatelliteFlow):
    async def async_step_user(self, user_input=None):
        return self.async_abort(reason="single_instance_allowed")


@pytest.mark.parametrize("upstream", ["renamed_field", "aborts", "no_entity"])
async def test_upstream_flow_change_fails_soft(hass, ks_api, upstream):
    """[KSM-TEST-192] Negative, the accepted risk: if Voice Satellite's flow
    changes shape, aborts, or yields no satellite entity, the device stays
    loaded and KS is not bound to anything."""
    _, patch_settings, _ = ks_api
    if upstream == "no_entity":
        mock_integration(hass, MockModule(VS), built_in=False)
        mock_platform(hass, f"{VS}.config_flow", None)
        flow = _VoiceSatelliteFlow
    else:
        _install_voice_satellite(hass)
        flow = _RenamedFieldFlow if upstream == "renamed_field" else _AbortingFlow
    with mock_config_flow(VS, flow):
        entry = await _add_device(hass, "Great Room Kiosk")
    assert entry.state is ConfigEntryState.LOADED
    assert _bound(patch_settings) == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None


async def test_binding_owned_by_another_devices_name_is_released(hass, ks_api):
    """[KSM-TEST-192] A renamed device (was "Great Room Kiosk", now "Master
    Bedroom Kiosk") still bound to the satellite whose name another KSM
    device now has gives it up: it gets its own entry, and the device
    with that name adopts the old one."""
    _install_voice_satellite(hass)
    old = MockConfigEntry(
        domain=VS, unique_id="great_room_kiosk", title="Great Room Kiosk",
        data={"name": "Great Room Kiosk"},
    )
    old.add_to_hass(hass)
    assert await hass.config_entries.async_setup(old.entry_id)
    old_satellite = _satellite(hass, old)
    _, patch_settings, get_settings = ks_api

    get_settings.return_value = {"ha.satellite_entity": old_satellite}
    await _add_device(hass, "Master Bedroom Kiosk", host="192.168.99.250")
    # Alone, the renamed device keeps its binding: no other device owns the name yet.
    assert _bound(patch_settings) == []

    get_settings.return_value = {}
    await _add_device(hass, "Great Room Kiosk", host="192.168.99.45")
    assert _bound(patch_settings) == [("192.168.99.45", old_satellite)]

    get_settings.return_value = {"ha.satellite_entity": old_satellite}
    [renamed] = [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("host") == "192.168.99.250"]
    assert await hass.config_entries.async_reload(renamed.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    [bedroom] = [e for e in hass.config_entries.async_entries(VS) if e.unique_id == "master_bedroom_kiosk"]
    assert _bound(patch_settings)[-1] == ("192.168.99.250", _satellite(hass, bedroom))
    assert len(hass.config_entries.async_entries(VS)) == 2
