"""KSM-TEST-328/329 (#135): a support request proposes a library entry for a
device KSM cannot provision, and pre-fills the GitHub issue form with it.

Reports come from the real `CapabilityReportCollector` over a fake ADB shell,
so the catalog resolution inside each request is the real catalog's answer.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from custom_components.kiosk_satellite_manager import support_request as sr
from custom_components.kiosk_satellite_manager.capability_report import CapabilityReportCollector
from custom_components.kiosk_satellite_manager.device_models import DeviceFacts, DeviceModel
from custom_components.kiosk_satellite_manager.install_recipes import INSTALL_RECIPES

_FORM = Path(__file__).resolve().parents[2] / ".github" / "ISSUE_TEMPLATE" / "device-support.yml"
_TRACKER = "https://github.com/cfoxga/kiosk-satellite-manager/issues"
_PORTAL_GO_FINGERPRINT = "facebook/terry_prod/terry:10/qkq1.210213.001/5051355900018050:user/prod-keys"


class _Shell:
    """Answers the collector's allowlisted probes from a prop table."""

    def __init__(self, props: dict[str, str]) -> None:
        self._props = props

    async def shell(self, command: str) -> str:
        if command.startswith("getprop "):
            return self._props.get(command.removeprefix("getprop "), "")
        return ""


async def _report(**props: str) -> dict:
    return await CapabilityReportCollector(_Shell(props)).collect()


async def _onn_streaming_box(**override: str) -> dict:
    props = {
        "ro.product.manufacturer": "onn", "ro.product.brand": "onn",
        "ro.product.model": "onn 4K Streaming Box", "ro.product.name": "dopinder",
        "ro.product.device": "dopinder", "ro.product.board": "sabrina",
        "ro.hardware": "amlogic", "ro.build.characteristics": "tv,nosdcard",
        "ro.product.cpu.abi": "armeabi-v7a", "ro.build.version.sdk": "31",
        "ro.build.fingerprint": "onn/dopinder/dopinder:12/STT2.1/1:user/release-keys",
    }
    props.update(override)
    return await _report(**props)


async def _portal(model: str, sdk: str, *, fingerprint: str = "", characteristics: str = "nosdcard") -> dict:
    return await _report(**{
        "ro.product.manufacturer": "Facebook", "ro.product.brand": "Facebook",
        "ro.product.model": model, "ro.product.device": "unknownportal",
        "ro.build.characteristics": characteristics, "ro.build.version.sdk": sdk,
        "ro.build.fingerprint": fingerprint,
    })


def _as_model(draft: dict) -> DeviceModel:
    """The draft is a DeviceModel row a maintainer could paste."""
    return DeviceModel(
        model_key=draft["model_key"], name=draft["name"],
        manufacturer=tuple(draft["manufacturer"]), models=tuple(draft["models"]),
        devices=tuple(draft.get("devices", ())),
        min_sdk=draft["min_sdk"], max_sdk=draft["max_sdk"],
    )


# --- KSM-TEST-328: request kind, device-model draft, candidate recipe -------

async def test_KSM_TEST_328_unmatched_tv_proposes_a_new_device_model_and_android_tv():
    report = await _onn_streaming_box()
    request = sr.build_support_request(report, ksm_version="0.4.2")

    assert request["kind"] == sr.KIND_NEW_DEVICE
    draft = request["device_model"]
    assert draft["model_key"] == "onn_4k_streaming_box_sdk31"
    assert draft["manufacturer"] == ["onn"]
    assert draft["models"] == ["onn 4k streaming box"]
    assert draft["devices"] == ["dopinder"]
    assert (draft["min_sdk"], draft["max_sdk"]) == (31, 31)
    assert draft["catalog_state"] == "provisional"
    assert draft["observed_facts"]["ro.product.board"] == "sabrina"
    assert draft["observed_facts"]["ro.build.fingerprint"].startswith("onn/dopinder/")
    assert request["recipe"] == {
        "candidate": "android_tv", "state": "proposed", "new_recipe_needed": False,
        "basis": request["recipe"]["basis"],
    }
    assert request["ksm_version"] == "0.4.2"
    # The draft matches the device it came from and nothing else of its kind.
    source = DeviceFacts.from_platform(report["facts"]["platform"])
    assert _as_model(draft).matches(source)
    other = DeviceFacts.from_platform({**report["facts"]["platform"], "model": "onn 4K Pro"})
    assert not _as_model(draft).matches(other)


async def test_KSM_TEST_328_an_unreported_fact_stays_out_of_the_match_rule():
    report = await _onn_streaming_box(**{"ro.product.device": ""})
    draft = sr.build_support_request(report, ksm_version="0.4.2")["device_model"]
    assert "devices" not in draft
    assert "" not in draft["manufacturer"] + draft["models"]
    assert "ro.product.device" not in draft["observed_facts"]


@pytest.mark.parametrize(("sdk", "expected"), [
    ("28", "meta_portal_android9_local_dns"),
    ("29", "meta_portal_android10_local_dns"),
])
async def test_KSM_TEST_328_unmatched_portal_picks_the_recipe_for_its_android(sdk, expected):
    request = sr.build_support_request(await _portal("Portal 3", sdk), ksm_version="0.4.2")
    assert request["kind"] == sr.KIND_NEW_DEVICE
    assert request["recipe"]["candidate"] == expected


async def test_KSM_TEST_328_a_portal_reporting_tv_picks_the_portal_tv_recipe():
    request = sr.build_support_request(
        await _portal("Portal TV 2", "29", characteristics="tv"), ksm_version="0.4.2"
    )
    assert request["recipe"]["candidate"] == "meta_portal_tv_local_dns"


async def test_KSM_TEST_328_a_non_tv_non_portal_device_needs_a_new_recipe():
    report = await _report(**{
        "ro.product.manufacturer": "Lenovo", "ro.product.model": "Smart Display 10",
        "ro.product.device": "blackjack", "ro.build.characteristics": "nosdcard",
        "ro.build.version.sdk": "27",
    })
    request = sr.build_support_request(report, ksm_version="0.4.2")
    assert request["kind"] == sr.KIND_NEW_DEVICE
    assert request["recipe"]["candidate"] is None
    assert request["recipe"]["new_recipe_needed"] is True


def test_KSM_TEST_328_every_candidate_names_a_real_recipe():
    recipe_keys = {recipe.recipe_key for recipe in INSTALL_RECIPES}
    assert set(sr.CANDIDATE_RECIPES) <= recipe_keys
    assert sr.CANDIDATE_RECIPES  # positive control: the mapping is not empty


async def test_KSM_TEST_328_a_supported_build_needs_nothing_and_a_new_build_is_named():
    supported = sr.build_support_request(
        await _portal("PortalGo", "29", fingerprint=_PORTAL_GO_FINGERPRINT), ksm_version="0.4.2"
    )
    assert supported["kind"] == sr.KIND_SUPPORTED
    assert supported["device_model"] is None

    new_build = sr.build_support_request(
        await _portal("PortalGo", "29", fingerprint="facebook/terry_prod/terry:10/NEWBUILD:user/prod-keys"),
        ksm_version="0.4.2",
    )
    assert new_build["kind"] == sr.KIND_NEW_BUILD
    assert new_build["existing_model_key"] == "portal_go"
    assert new_build["device_model"] is None
    assert new_build["recipe"]["current"] == "meta_portal_android10_local_dns"


# --- KSM-TEST-329: the pre-filled issue URL ---------------------------------

def _query(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{_TRACKER}/new"
    return {key: values[0] for key, values in parse_qs(parts.query).items()}


async def test_KSM_TEST_329_the_url_prefills_the_issue_form():
    request = sr.build_support_request(await _onn_streaming_box(), ksm_version="0.4.2")
    url = sr.issue_url(request, _TRACKER)
    query = _query(url)

    assert query["template"] == "device-support.yml"
    assert query["title"] == "Device support: onn 4K Streaming Box (SDK 31)"
    assert query["device"] == "onn 4K Streaming Box (SDK 31)"
    proposal = json.loads(query["proposal"])
    assert proposal["device_model"]["model_key"] == "onn_4k_streaming_box_sdk31"
    assert "report" not in proposal
    assert json.loads(query["report"]) == request["report"]
    assert len(url) <= sr.MAX_URL_LENGTH


async def test_KSM_TEST_329_an_oversized_report_is_dropped_for_diagnostics():
    report = await _onn_streaming_box(**{"ro.build.fingerprint": "x" * 9000})
    request = sr.build_support_request(report, ksm_version="0.4.2")
    url = sr.issue_url(request, _TRACKER)
    query = _query(url)

    assert "report" not in query
    assert len(url) <= sr.MAX_URL_LENGTH
    assert "Download diagnostics" in json.loads(query["proposal"])["attach"]
    # Control: the same request with a small report keeps it and has no note.
    small = sr.build_support_request(await _onn_streaming_box(), ksm_version="0.4.2")
    small_query = _query(sr.issue_url(small, _TRACKER))
    assert "report" in small_query and "attach" not in json.loads(small_query["proposal"])


async def test_KSM_TEST_329_no_host_name_or_secret_reaches_the_request():
    request = sr.build_support_request(await _onn_streaming_box(), ksm_version="0.4.2")
    blob = json.dumps(request) + sr.issue_url(request, _TRACKER)
    for secret in ("192.168.99.99", "Kitchen Display", "synthetic-test-password"):
        assert secret not in blob
    # Positive control: the instrument does see real facts.
    assert "dopinder" in blob


def test_KSM_TEST_329_every_prefilled_field_is_declared_by_the_issue_form():
    form = yaml.safe_load(_FORM.read_text())
    ids = {item["id"] for item in form["body"] if "id" in item}
    assert set(sr.FORM_FIELDS) <= ids
    assert {"device", "proposal", "report"} <= set(sr.FORM_FIELDS)
