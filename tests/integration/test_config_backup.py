"""Device configuration backup and restore (KSM-BEHAVE-104/105/106,
kiosk-satellite-manager#69)."""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.kiosk_satellite_manager import config_backup
from custom_components.kiosk_satellite_manager.const import (
    CONF_BACKUP_INTERVAL_HOURS,
    CONF_BACKUP_KEEP,
    CONF_HA_URL,
    CONF_TOKEN_MODE,
    DOMAIN,
    TOKEN_MODE_AUTO,
)
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

from .conftest import init_integration
from .test_global_settings import _entity, _manager

_API = "custom_components.kiosk_satellite_manager.ks_api_client"
_HEALTH = "custom_components.kiosk_satellite_manager.fetch_health"


def _export(exported_at="2026-09-28T10:00:00", **settings):
    base = {"device.name": "Test Kiosk", "remote.password": "old-pw", "ha.token": "old-token"}
    base.update(settings)
    return {
        "kind": "kiosk-satellite-config",
        "version": 1,
        "deviceName": base["device.name"],
        "exportedAt": exported_at,
        "settings": base,
        "localStorage": "{}",
    }


@pytest.fixture
def config_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    return tmp_path


@pytest.fixture
def ks_api():
    with patch(f"{_API}.login", new=AsyncMock(return_value="tok")) as login, patch(
        f"{_API}.export_config", new=AsyncMock(return_value=_export())
    ) as export, patch(
        f"{_API}.import_config", new=AsyncMock(return_value={"ok": True, "data": {"applied": 3}})
    ) as imp:
        yield type("Api", (), {"login": login, "export": export, "imp": imp})


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 28, 10, 0, 0, tzinfo=dt_util.get_default_time_zone())

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    c = _Clock()
    with patch.object(config_backup, "_now", new=c):
        yield c


async def _device(hass, **kw):
    with patch(_HEALTH, new=AsyncMock(return_value={"appVersion": "2026.9.1"})):
        return (await init_integration(hass, **kw)).entry


def _files(hass, entry):
    d = config_backup.backup_dir(hass, entry)
    return sorted(os.listdir(d)) if d.exists() else []


async def _press(hass, entity_id):
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()


async def test_backup_writes_export_unchanged_to_dated_private_file(
    hass, config_dir, ks_api, clock
):
    """[KSM-TEST-199] Dated 0600 file under the entry's dir, payload unchanged."""
    entry = await _device(hass)
    await _press(hass, _entity(hass, entry, "backup_config"))
    [name] = _files(hass, entry)
    assert name == f"{config_backup.slugify(entry.title)}_2026-09-28_10-00-00.json"
    path = config_backup.backup_dir(hass, entry) / name
    assert path.parent == config_dir / DOMAIN / "backups" / entry.entry_id
    assert json.loads(path.read_text()) == _export()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    ks_api.login.assert_awaited_once_with(
        ks_api.login.await_args.args[0], entry.data["host"], "synthetic-test-password", pin=None
    )


@pytest.mark.parametrize(
    "failure",
    [
        {"kind": "something-else", "settings": {}},
        {"kind": "kiosk-satellite-config", "settings": "nope"},
        KsApiError("boom"),
    ],
)
async def test_backup_failure_writes_nothing(hass, config_dir, ks_api, clock, failure):
    """[KSM-TEST-199] Malformed export or KS error raises; nothing is written."""
    entry = await _device(hass)
    if isinstance(failure, Exception):
        ks_api.export.side_effect = failure
    else:
        ks_api.export.return_value = failure
    with pytest.raises(HomeAssistantError, match=entry.title):
        await config_backup.async_backup_entry(hass, entry)
    assert _files(hass, entry) == []


async def test_backup_without_password_raises(hass, config_dir, ks_api, clock):
    """[KSM-TEST-199] No stored password: refuse before any request."""
    entry = await _device(hass, data={"password": ""})
    with pytest.raises(HomeAssistantError, match="password"):
        await config_backup.async_backup_entry(hass, entry)
    ks_api.login.assert_not_awaited()
    assert _files(hass, entry) == []


async def test_identical_backup_replaces_previous_distinct_backup_is_kept(
    hass, config_dir, ks_api, clock
):
    """[KSM-TEST-200] Only exportedAt differs -> old copy deleted; a setting differs -> kept."""
    entry = await _device(hass)
    slug = config_backup.slugify(entry.title)
    await config_backup.async_backup_entry(hass, entry)
    clock.advance(hours=1)
    ks_api.export.return_value = _export(exported_at="2026-09-28T11:00:00")
    await config_backup.async_backup_entry(hass, entry)
    assert _files(hass, entry) == [f"{slug}_2026-09-28_11-00-00.json"]

    clock.advance(hours=1)
    ks_api.export.return_value = _export(exported_at="2026-09-28T12:00:00", **{"browser.zoom": 2})
    await config_backup.async_backup_entry(hass, entry)
    assert _files(hass, entry) == [
        f"{slug}_2026-09-28_11-00-00.json",
        f"{slug}_2026-09-28_12-00-00.json",
    ]


async def test_retention_keeps_newest_n_and_ignores_foreign_files(
    hass, config_dir, ks_api, clock
):
    """[KSM-TEST-201] Backups to keep = 2; a non-pattern file is untouched."""
    await _manager(hass, options={CONF_BACKUP_KEEP: 2})
    entry = await _device(hass)
    directory = config_backup.backup_dir(hass, entry)
    directory.mkdir(parents=True)
    (directory / "notes.json").write_text("{}")
    for i in range(4):
        ks_api.export.return_value = _export(**{"browser.zoom": i})
        await config_backup.async_backup_entry(hass, entry)
        clock.advance(hours=1)
    slug = config_backup.slugify(entry.title)
    assert _files(hass, entry) == [
        f"{slug}_2026-09-28_12-00-00.json",
        f"{slug}_2026-09-28_13-00-00.json",
        "notes.json",
    ]


async def test_manager_options_backup_defaults_and_validation(hass):
    """[KSM-TEST-202] Defaults 24 h / 10; keep 0 and negative period rejected."""
    manager = await _manager(hass)
    result = await hass.config_entries.options.async_init(manager.entry_id)
    data = result["data_schema"]({})
    assert data[CONF_BACKUP_INTERVAL_HOURS] == 24
    assert data[CONF_BACKUP_KEEP] == 10
    for bad in ({CONF_BACKUP_KEEP: 0}, {CONF_BACKUP_INTERVAL_HOURS: -1}):
        with pytest.raises(vol.Invalid):
            result["data_schema"]({**data, **bad})
    data.update({
        CONF_HA_URL: "https://ha.example.test", CONF_TOKEN_MODE: TOKEN_MODE_AUTO,
        CONF_BACKUP_INTERVAL_HOURS: 6, CONF_BACKUP_KEEP: 3,
    })
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert manager.options[CONF_BACKUP_INTERVAL_HOURS] == 6
    assert manager.options[CONF_BACKUP_KEEP] == 3


async def test_scheduled_check_backs_up_only_due_devices(hass, config_dir, ks_api, clock):
    """[KSM-TEST-203] No backup / stale backup -> backed up; fresh -> skipped; 0 -> none."""
    manager = await _manager(hass, options={CONF_BACKUP_INTERVAL_HOURS: 6})
    fresh = await _device(hass, data={"host": "10.0.0.1"})
    stale = await _device(hass, data={"host": "10.0.0.2"})
    never = await _device(hass, data={"host": "10.0.0.3"})

    await config_backup.async_backup_entry(hass, stale)
    clock.advance(hours=5)
    await config_backup.async_backup_entry(hass, fresh)
    clock.advance(hours=1, seconds=1)

    ks_api.export.reset_mock()
    ks_api.export.return_value = _export(**{"browser.zoom": 9})
    await config_backup.async_run_due_backups(hass)
    hosts = sorted(c.args[1] for c in ks_api.export.await_args_list)
    assert hosts == ["10.0.0.2", "10.0.0.3"]
    assert len(_files(hass, stale)) == 2 and len(_files(hass, never)) == 1
    assert len(_files(hass, fresh)) == 1

    hass.config_entries.async_update_entry(
        manager, options={**manager.options, CONF_BACKUP_INTERVAL_HOURS: 0}
    )
    clock.advance(days=30)
    ks_api.export.reset_mock()
    await config_backup.async_run_due_backups(hass)
    ks_api.export.assert_not_awaited()


async def test_scheduled_check_survives_one_device_failing(hass, config_dir, ks_api, clock):
    """[KSM-TEST-203] One device's error is logged; the others still back up."""
    await _manager(hass)
    bad = await _device(hass, data={"host": "10.0.0.8"})
    good = await _device(hass, data={"host": "10.0.0.9"})

    async def _export_by_host(session, host, token, *, pin):
        if host == "10.0.0.8":
            raise KsApiError("offline")
        return _export()

    ks_api.export.side_effect = _export_by_host
    await config_backup.async_run_due_backups(hass)
    assert _files(hass, bad) == []
    assert len(_files(hass, good)) == 1


async def test_scheduled_check_skips_device_health_reports_unreachable(
    hass, config_dir, ks_api, clock
):
    """[KSM-TEST-203] An offline device is skipped, not retried with a warning per tick."""
    await _manager(hass)
    offline = await _device(hass, data={"host": "10.0.0.8"})
    online = await _device(hass, data={"host": "10.0.0.9"})
    hass.data[DOMAIN][offline.entry_id].last_update_success = False
    await config_backup.async_run_due_backups(hass)
    assert [c.args[1] for c in ks_api.export.await_args_list] == ["10.0.0.9"]
    assert _files(hass, offline) == [] and len(_files(hass, online)) == 1
    # A manual press still tries, and reports the failure.
    ks_api.export.side_effect = KsApiError("offline")
    with pytest.raises(HomeAssistantError, match=offline.title):
        await config_backup.async_backup_entry(hass, offline)


async def test_scheduled_check_runs_from_manager_timer(hass, config_dir, ks_api, clock):
    """[KSM-TEST-203] The manager entry drives the check on its interval."""
    with patch.object(config_backup, "async_run_due_backups", new=AsyncMock()) as run:
        manager = await _manager(hass)
        run.assert_not_awaited()
        from pytest_homeassistant_custom_component.common import async_fire_time_changed

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
        await hass.async_block_till_done()
        assert run.await_count == 1
        await hass.config_entries.async_unload(manager.entry_id)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=40))
        await hass.async_block_till_done()
        assert run.await_count == 1


async def test_restore_imports_selected_backup_with_current_credentials(
    hass, config_dir, ks_api, clock
):
    """[KSM-TEST-204] Select lists newest first; restore overlays live credentials."""
    entry = await _device(hass)
    select = _entity(hass, entry, "config_backup")
    ks_api.export.return_value = _export(**{"browser.zoom": 1})
    await _press(hass, _entity(hass, entry, "backup_config"))
    clock.advance(hours=1)
    ks_api.export.return_value = _export(**{"browser.zoom": 2})
    await _press(hass, _entity(hass, entry, "backup_config"))
    slug = config_backup.slugify(entry.title)
    older, newer = f"{slug}_2026-09-28_10-00-00.json", f"{slug}_2026-09-28_11-00-00.json"
    state = hass.states.get(select)
    assert state.attributes["options"] == [newer, older]
    assert state.state == newer

    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": older}, blocking=True
    )
    clock.advance(hours=1)
    ks_api.export.return_value = _export(
        **{"browser.zoom": 3, "remote.password": "live-pw", "ha.token": "live-token"}
    )
    health = AsyncMock(return_value={"appVersion": "2026.9.1"})
    with patch(_HEALTH, new=health):
        await _press(hass, _entity(hass, entry, "restore_config"))
        await hass.async_block_till_done(wait_background_tasks=True)
    health.assert_awaited()  # the entry's health refresh ran after the import

    ks_api.imp.assert_awaited_once()
    sent = ks_api.imp.await_args.args[3]
    assert sent["settings"]["browser.zoom"] == 1
    assert sent["settings"]["remote.password"] == "live-pw"
    assert sent["settings"]["ha.token"] == "live-token"
    assert ks_api.imp.await_args.kwargs == {"pin": None}
    # The pre-restore safety backup of zoom=3 exists and is now the newest option.
    pre = f"{slug}_2026-09-28_12-00-00.json"
    assert pre in _files(hass, entry)
    assert hass.states.get(select).attributes["options"][0] == pre


async def test_restore_failures_and_select_rejects_unknown_option(
    hass, config_dir, ks_api, clock
):
    """[KSM-TEST-205] No backups / KS rejection raise; foreign options rejected."""
    entry = await _device(hass)
    with pytest.raises(HomeAssistantError, match="no configuration backup"):
        await _press(hass, _entity(hass, entry, "restore_config"))
    ks_api.imp.assert_not_awaited()

    await config_backup.async_backup_entry(hass, entry)
    for bad in ("../../secrets.yaml", "nope.json"):
        with pytest.raises((ServiceValidationError, HomeAssistantError, vol.Invalid)):
            await hass.services.async_call(
                "select", "select_option",
                {"entity_id": _entity(hass, entry, "config_backup"), "option": bad},
                blocking=True,
            )
    with pytest.raises(HomeAssistantError):
        await config_backup.async_restore_entry(hass, entry, "../../secrets.yaml")

    ks_api.imp.side_effect = KsApiError("import rejected")
    with pytest.raises(HomeAssistantError, match="import rejected"):
        await _press(hass, _entity(hass, entry, "restore_config"))
