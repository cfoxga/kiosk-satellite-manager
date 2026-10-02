"""KSM-TEST-261..264 (#96): the add wizard's Enable ESPHome checkbox and the
step that adds the kiosk to HA's ESPHome integration with KS's encryption key."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kiosk_satellite_manager import esphome_adopt
from custom_components.kiosk_satellite_manager.const import (
    CONF_ENABLE_ESPHOME,
    CONF_ESPHOME_ENABLE_PENDING,
    CONF_ESPHOME_NEW_DEVICES,
    CONF_HOST,
    CONF_PASSWORD,
    DOMAIN,
)
from custom_components.kiosk_satellite_manager.ks_api_client import KsApiError

KS = "custom_components.kiosk_satellite_manager.ks_api_client"
FLOW = "custom_components.kiosk_satellite_manager.config_flow"
HOST = "192.0.2.77"
NODE = "great-room-kiosk"
KEY = "SYNTHETIC+KEY+FOR+TESTS+0123456789ABCDEF="


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(esphome_adopt, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(esphome_adopt, "KEY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(esphome_adopt, "DISCOVERY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(esphome_adopt, "LOAD_TIMEOUT_S", 0.2)


@pytest.fixture
def ks():
    """A fake KS API. `btproxy.key` appears once ESPHome has been enabled and
    two further settings reads have passed, as on a real device."""
    state = {
        "esphome.enabled": False,
        "esphome.entities": False,
        "esphome.node_name": "",
        "btproxy.key": "",
        "reads": 0,
    }

    async def get_settings(*a, **k):
        if state["esphome.enabled"]:
            state["reads"] += 1
            if state["reads"] >= 2:
                state["btproxy.key"] = KEY
        return dict(state)

    async def patch_settings(session, host, token, payload, pin=None):
        state.update(payload)
        return {}

    patch_mock = AsyncMock(side_effect=patch_settings)
    with patch(f"{KS}.login", new=AsyncMock(return_value="tok")), patch(
        f"{KS}.get_settings", new=AsyncMock(side_effect=get_settings)
    ), patch(f"{KS}.patch_settings", new=patch_mock):
        yield state, patch_mock


class FakeFlows:
    """Stands in for HA's flow manager on the `esphome` handler."""

    def __init__(self, hass, progress, results):
        self.progress = progress
        self.results = list(results)
        self.configured: list[tuple[str, dict]] = []
        self._hass = hass

    def install(self):
        manager = self._hass.config_entries.flow
        manager.async_progress_by_handler = lambda handler, **kw: (
            list(self.progress) if handler == "esphome" else []
        )

        async def configure(flow_id, user_input=None):
            self.configured.append((flow_id, user_input))
            result = self.results.pop(0)
            if callable(result):
                result = result()
            return result

        manager.async_configure = configure


def _flow(name=NODE, step="discovery_confirm", flow_id="f1"):
    return {
        "flow_id": flow_id,
        "handler": "esphome",
        "step_id": step,
        "context": {"source": "zeroconf", "title_placeholders": {"name": name}},
    }


def _form(step, errors=None):
    return {"type": FlowResultType.FORM, "step_id": step, "errors": errors or {}}


def _created(hass):
    def make():
        entry = MockConfigEntry(domain="esphome", data={"host": HOST, "port": 6053})
        entry.add_to_hass(hass)
        entry.mock_state(hass, config_entries.ConfigEntryState.LOADED)
        return {"type": FlowResultType.CREATE_ENTRY}

    return make


async def _adopt(hass, name="Great Room Kiosk"):
    return await esphome_adopt.async_adopt(
        hass, host=HOST, name=name, password="pw", pin=None
    )


async def test_adopt_enables_esphome_waits_for_the_key_and_adds_the_device(hass, ks):
    """[KSM-TEST-262/263] One PATCH turns ESPHome on with the node name, the
    driver waits for the key, then confirms the discovery flow and submits the
    key to its encryption step."""
    state, patch_settings = ks
    flows = FakeFlows(
        hass,
        [_flow()],
        [_form("encryption_key"), _created(hass)],
    )
    flows.install()

    assert await _adopt(hass) == esphome_adopt.ADDED

    assert [c.args[3] for c in patch_settings.await_args_list] == [
        {"esphome.node_name": NODE, "esphome.enabled": True, "esphome.entities": True}
    ]
    assert flows.configured == [("f1", {}), ("f1", {"noise_psk": KEY})]


async def test_adopt_turns_entities_on_when_only_the_server_runs(hass, ks):
    """[KSM-TEST-298] ESPHome server on but entities off (Test Portal Plus on
    dev, #116) gives HA an ESPHome device with no entities: the PATCH turns
    entities on and leaves the rest alone."""
    state, patch_settings = ks
    state.update({"esphome.enabled": True, "esphome.node_name": NODE})
    FakeFlows(hass, [_flow()], [_form("encryption_key"), _created(hass)]).install()

    assert await _adopt(hass) == esphome_adopt.ADDED
    assert [c.args[3] for c in patch_settings.await_args_list] == [{"esphome.entities": True}]


async def test_adopt_leaves_settings_that_are_already_right(hass, ks):
    """[KSM-TEST-262] Negative: ESPHome already on with a chosen node name means
    no PATCH at all."""
    state, patch_settings = ks
    state.update(
        {"esphome.enabled": True, "esphome.entities": True, "esphome.node_name": "my-own-node"}
    )
    FakeFlows(hass, [_flow("my-own-node")], [_form("encryption_key"), _created(hass)]).install()

    assert await _adopt(hass) == esphome_adopt.ADDED
    patch_settings.assert_not_awaited()


@pytest.mark.parametrize("shown", [f"Great Room Kiosk ({NODE})", f"great room kiosk ({NODE.upper()})"])
async def test_a_discovery_titled_with_a_friendly_name_is_matched(hass, ks, shown):
    """[KSM-TEST-263] HA titles a discovery "<friendly name> (<node>)" when the
    device advertises a friendly name; the node in parentheses must still match."""
    flows = FakeFlows(hass, [_flow(shown)], [_form("encryption_key"), _created(hass)])
    flows.install()
    assert await _adopt(hass) == esphome_adopt.ADDED
    assert flows.configured[0][0] == "f1"


async def test_a_node_name_that_only_prefixes_another_is_not_matched(hass, ks):
    """[KSM-TEST-263] Negative control: the parenthesised part must equal the node."""
    flows = FakeFlows(hass, [_flow(f"Other ({NODE}-2)", flow_id="other")], [])
    flows.install()
    assert await _adopt(hass) == esphome_adopt.DISCOVERY_TIMEOUT
    assert flows.configured == []


async def test_a_key_that_never_appears_times_out_without_touching_a_flow(hass, ks):
    """[KSM-TEST-262] Negative: no `btproxy.key` -> key_timeout, no flow
    configured."""
    state, _ = ks
    flows = FakeFlows(hass, [_flow()], [])
    flows.install()
    with patch(f"{KS}.get_settings", new=AsyncMock(return_value={"esphome.enabled": True, "esphome.node_name": NODE, "btproxy.key": ""})):
        assert await _adopt(hass) == esphome_adopt.KEY_TIMEOUT
    assert flows.configured == []


async def test_ks_failure_is_reported_and_touches_no_flow(hass, ks):
    """[KSM-TEST-264] Negative: a KS rejection reports failure."""
    flows = FakeFlows(hass, [_flow()], [])
    flows.install()
    with patch(f"{KS}.login", new=AsyncMock(side_effect=KsApiError("bad password"))):
        assert await _adopt(hass) == esphome_adopt.FAILED
    assert flows.configured == []


async def test_a_flow_for_another_node_is_never_configured(hass, ks):
    """[KSM-TEST-263] Negative control: only the discovery for this node name is
    submitted; a neighbour's flow is left alone."""
    flows = FakeFlows(hass, [_flow("someone-elses-node", flow_id="other")], [])
    flows.install()
    assert await _adopt(hass) == esphome_adopt.DISCOVERY_TIMEOUT
    assert flows.configured == []


async def test_a_rejected_key_is_a_failure(hass, ks):
    """[KSM-TEST-263] Negative: HA re-showing the encryption step with an error
    is a failure, and the key is submitted once."""
    flows = FakeFlows(
        hass,
        [_flow()],
        [_form("encryption_key"), _form("encryption_key", {"base": "invalid_psk"})],
    )
    flows.install()
    assert await _adopt(hass) == esphome_adopt.FAILED
    assert len(flows.configured) == 2


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("already_configured", esphome_adopt.ALREADY_ADDED), ("reauth_successful", esphome_adopt.FAILED)],
)
async def test_an_abort_succeeds_only_when_already_configured(hass, ks, reason, expected):
    """[KSM-TEST-263] `already_configured` means HA has the device; any other
    abort reason is a failure."""
    FakeFlows(
        hass, [_flow()], [{"type": FlowResultType.ABORT, "reason": reason}]
    ).install()
    assert await _adopt(hass) == expected


async def test_an_entry_that_never_loads_is_a_failure(hass, ks):
    """[KSM-TEST-263] Negative: a created flow with no loaded ESPHome entry at
    the kiosk's IP within the wait is a failure, not a success."""
    FakeFlows(
        hass, [_flow()], [_form("encryption_key"), {"type": FlowResultType.CREATE_ENTRY}]
    ).install()
    assert await _adopt(hass) == esphome_adopt.FAILED


async def test_an_existing_esphome_entry_short_circuits(hass, ks):
    """[KSM-TEST-264] An enabled ESPHome entry already at the kiosk's IP: no
    flow is submitted and KSM does not edit it."""
    ks[0]["esphome.entities"] = True
    entry = MockConfigEntry(domain="esphome", data={"host": HOST, "port": 6053, "noise_psk": "x"})
    entry.add_to_hass(hass)
    flows = FakeFlows(hass, [_flow()], [])
    flows.install()

    assert await _adopt(hass) == esphome_adopt.ALREADY_ADDED
    assert flows.configured == []
    assert entry.data["noise_psk"] == "x"


@pytest.mark.parametrize(("entities_on", "reloaded"), [(False, True), (True, False)])
async def test_an_existing_entry_is_reloaded_only_when_entities_were_just_turned_on(
    hass, ks, entities_on, reloaded
):
    """[KSM-TEST-298] HA lists an ESPHome device's entities only on connect, so
    an existing entry is reloaded after the PATCH turns entities on; with
    entities already on it is left alone. Neither path edits the entry."""
    state, _ = ks
    state.update({"esphome.enabled": True, "esphome.entities": entities_on, "esphome.node_name": NODE})
    entry = MockConfigEntry(domain="esphome", data={"host": HOST, "port": 6053, "noise_psk": "x"})
    entry.add_to_hass(hass)
    FakeFlows(hass, [_flow()], []).install()

    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload:
        assert await _adopt(hass) == esphome_adopt.ALREADY_ADDED

    assert reload.await_args_list == ([call(entry.entry_id)] if reloaded else [])
    assert entry.data["noise_psk"] == "x"


async def test_a_flow_that_keeps_asking_is_a_failure(hass, ks):
    """[KSM-TEST-263] Negative: a flow still showing a form after the step
    limit is abandoned, not looped forever."""
    FakeFlows(hass, [_flow()], [_form("encryption_key")] * 10).install()
    assert await _adopt(hass) == esphome_adopt.FAILED


async def test_an_unresolvable_host_matches_no_entry(hass):
    """[KSM-TEST-264] Negative: a host that resolves to no IP has no entries."""
    with patch.object(esphome_adopt, "_host_ip", new=AsyncMock(return_value=None)):
        assert await esphome_adopt._entries_at(hass, "nowhere.invalid") == []


# --- the wizard -----------------------------------------------------------


def _field(result, name):
    return next(k for k in result["data_schema"].schema if k == name)


async def _wizard(hass, *, option, box=None, adopt=esphome_adopt.ADDED, notify=None):
    """Adopt an already-running KS through the real config flow; returns
    (result, adopt mock)."""
    manager = {} if option is None else {CONF_ESPHOME_NEW_DEVICES: option}
    adopt_mock = AsyncMock(side_effect=adopt) if isinstance(adopt, Exception) else AsyncMock(return_value=adopt)
    with patch(f"{FLOW}._manager_options", return_value=manager), patch(
        f"{FLOW}.login", new=AsyncMock(return_value="tok")
    ), patch(f"{FLOW}.esphome_adopt.async_adopt", new=adopt_mock), patch(
        f"{FLOW}.persistent_notification.async_create", new=notify or MagicMock()
    ), patch(
        "custom_components.kiosk_satellite_manager.fetch_health",
        new=AsyncMock(return_value={"appVersion": "2026.9.91"}),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.0.2.77", "port": 5555}
        )
        assert result["step_id"] == "ks_device_info"
        default = _field(result, CONF_ENABLE_ESPHOME).default()
        answer = {CONF_PASSWORD: "hunter222"}
        if box is not None:
            answer[CONF_ENABLE_ESPHOME] = box
        result = await hass.config_entries.flow.async_configure(result["flow_id"], answer)
        while result["type"] == FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        await hass.async_block_till_done()
    return result, adopt_mock, default


@pytest.fixture
def running_ks(ks_health_probe, tls_migration):
    ks_health_probe.return_value = (None, {"appVersion": "2026.9.91", "name": "Great Room Kiosk"})
    tls_migration.return_value = None


@pytest.mark.parametrize(
    ("option", "default"), [(True, True), (False, False), (None, False)]
)
async def test_the_checkbox_defaults_to_the_manager_option(hass, running_ks, option, default):
    """[KSM-TEST-261] Enable ESPHome defaults to the manager option; off or no
    manager entry -> off."""
    _, _, seen = await _wizard(hass, option=option)
    assert seen is default


@pytest.mark.parametrize(("option", "box"), [(False, True), (True, False)])
async def test_the_checkbox_not_the_option_decides_the_new_entry(hass, running_ks, option, box):
    """[KSM-TEST-261] Negative control: a ticked box with the option off is
    pending and adopts; an unticked box with the option on is neither."""
    result, adopt, _ = await _wizard(hass, option=option, box=box)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ESPHOME_ENABLE_PENDING] is box
    assert adopt.await_count == (1 if box else 0)


async def test_the_wizard_adopts_with_the_device_credentials(hass, running_ks):
    """[KSM-TEST-262] The ticked wizard hands the adopt step the device's host,
    name and the password just entered."""
    result, adopt, _ = await _wizard(hass, option=True)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    kwargs = adopt.await_args.kwargs
    assert (kwargs["host"], kwargs["name"], kwargs["password"]) == (
        "192.0.2.77",
        "Great Room Kiosk",
        "hunter222",
    )


@pytest.mark.parametrize(
    "outcome",
    [
        esphome_adopt.KEY_TIMEOUT,
        esphome_adopt.DISCOVERY_TIMEOUT,
        esphome_adopt.FAILED,
    ],
)
async def test_a_failed_adopt_still_creates_the_entry_and_notifies_without_the_key(
    hass, running_ks, outcome
):
    """[KSM-TEST-264] Negative: every failure creates the KSM entry, keeps the
    pending flag and raises a notification that never contains the key."""
    notify = MagicMock()
    result, _, _ = await _wizard(hass, option=True, adopt=outcome, notify=notify)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ESPHOME_ENABLE_PENDING] is True
    messages = [c.kwargs.get("message", "") for c in notify.call_args_list]
    assert any("ESPHome" in m for m in messages)
    assert not any(KEY in m for m in messages)


@pytest.mark.parametrize("outcome", [esphome_adopt.ADDED, esphome_adopt.ALREADY_ADDED])
async def test_a_successful_adopt_raises_no_esphome_notification(hass, running_ks, outcome):
    """[KSM-TEST-264] Control: success is silent."""
    notify = MagicMock()
    await _wizard(hass, option=True, adopt=outcome, notify=notify)
    assert not any("ESPHome" in c.kwargs.get("message", "") for c in notify.call_args_list)


async def test_an_adopt_that_raises_still_creates_the_entry(hass, running_ks):
    """[KSM-TEST-262] An unexpected adopt exception is reported, not fatal."""
    notify = MagicMock()
    result, _, _ = await _wizard(hass, option=True, adopt=RuntimeError("boom"), notify=notify)
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    notify.assert_called_once()
    assert "boom" not in str(notify.call_args)
