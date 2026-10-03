"""Device support requests (KSM-BEHAVE-164..166, #135).

A support request turns one capability report into a proposal a maintainer
can turn into catalog rows: a `DeviceModel` draft for a device the library
does not know, and a candidate recipe from an existing family. It never adds,
approves or executes anything -- the device stays refused until a maintainer
ships the rows (`docs/developer/android-support/profile-workflow.md`).

The request becomes a pre-filled GitHub issue-form URL. It carries no host,
address, entry title, device name or credential.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote, urlencode

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .adb_client import AdbClient
from .capability_report import CapabilityReportCollector
from .const import CONF_HOST, CONF_KEY_PATH, CONF_PORT, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .fleet import DeviceEntry

KIND_NEW_DEVICE: Final = "new_device"
KIND_NEW_RECIPE: Final = "new_recipe"
KIND_NEW_BUILD: Final = "new_build"
KIND_SUPPORTED: Final = "supported"

ISSUE_FORM: Final = "device-support.yml"
# Field ids of src/.github/ISSUE_TEMPLATE/device-support.yml the URL pre-fills.
FORM_FIELDS: Final = ("device", "proposal", "report", "ksm_version")
MAX_URL_LENGTH: Final = 8000
ATTACH_NOTE: Final = (
    "The capability report was too long for the link. Attach this device's Download "
    "diagnostics (Settings > Devices & services > Kiosk Satellite Manager > the device > "
    "Download diagnostics); it carries the full request."
)

_PORTAL_ANDROID9: Final = "meta_portal_android9_local_dns"
_PORTAL_ANDROID10: Final = "meta_portal_android10_local_dns"
_PORTAL_TV: Final = "meta_portal_tv_local_dns"
_ANDROID_TV: Final = "android_tv"
CANDIDATE_RECIPES: Final = (_PORTAL_ANDROID9, _PORTAL_ANDROID10, _PORTAL_TV, _ANDROID_TV)

_PORTAL_MANUFACTURERS: Final = frozenset({"facebook", "meta"})
_UNQUALIFIED_BUILD: Final = frozenset(
    {"recipe_assigned", "partially_qualified", "revalidation_required", "blocked"}
)
# A device answers getprop with whatever it likes; keep every value short
# enough that the proposal always fits the URL once the report is dropped.
_MAX_FACT: Final = 256
# Report key -> the getprop it was read from, for `observed_facts`.
_OBSERVED: Final = (
    ("manufacturer", "ro.product.manufacturer"),
    ("brand", "ro.product.brand"),
    ("model", "ro.product.model"),
    ("product", "ro.product.name"),
    ("codename", "ro.product.device"),
    ("board", "ro.product.board"),
    ("hardware", "ro.hardware"),
    ("characteristics", "ro.build.characteristics"),
    ("abi", "ro.product.cpu.abi"),
    ("sdk", "ro.build.version.sdk"),
    ("fingerprint", "ro.build.fingerprint"),
)
_STORE_KEY: Final = f"{DOMAIN}_support_requests"

_LOGGER = logging.getLogger(__name__)


def _clip(value: Any) -> str:
    return str(value or "").strip()[:_MAX_FACT]


def _sdk(platform: dict) -> int | None:
    sdk = platform.get("sdk")
    return sdk if isinstance(sdk, int) and sdk > 0 else None


def _display_name(platform: dict) -> str:
    manufacturer, model = _clip(platform.get("manufacturer")), _clip(platform.get("model"))
    if not model:
        return manufacturer or "Unknown Android device"
    if not manufacturer or model.lower().startswith(manufacturer.lower()):
        return model
    return f"{manufacturer} {model}"


def device_label(request: dict) -> str:
    """`onn 4K Streaming Box (SDK 31)` -- the device as the issue names it."""
    platform = request["report"]["facts"]["platform"]
    sdk = _sdk(platform)
    name = _display_name(platform)
    return f"{name} (SDK {sdk})" if sdk else name


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _device_model_draft(platform: dict) -> dict:
    """A `DeviceModel` row a maintainer can paste, matching only what was reported."""
    sdk = _sdk(platform)
    key = _slug(_display_name(platform)) or "unknown_android_device"
    draft: dict[str, Any] = {
        "model_key": f"{key}_sdk{sdk}" if sdk else key,
        "name": _display_name(platform),
    }
    for field, fact in (("manufacturer", "manufacturer"), ("models", "model"), ("devices", "codename")):
        value = _clip(platform.get(fact)).lower()
        if value:
            draft[field] = [value]
    draft.setdefault("manufacturer", [])
    draft.setdefault("models", [])
    draft["min_sdk"] = sdk
    draft["max_sdk"] = sdk
    draft["catalog_state"] = "provisional"
    draft["observed_facts"] = {
        prop: _clip(platform.get(key)) for key, prop in _OBSERVED if _clip(platform.get(key))
    }
    return draft


def _candidate_recipe(platform: dict) -> dict:
    manufacturer = _clip(platform.get("manufacturer")).lower()
    characteristics = {
        part.strip() for part in _clip(platform.get("characteristics")).lower().split(",")
    }
    is_tv = "tv" in characteristics
    sdk = _sdk(platform)
    candidate: str | None = None
    if manufacturer in _PORTAL_MANUFACTURERS:
        if is_tv:
            candidate, basis = _PORTAL_TV, "a Meta Portal reporting the tv characteristic"
        elif sdk == 28:
            candidate, basis = _PORTAL_ANDROID9, "a Meta Portal on Android 9 (SDK 28)"
        elif sdk is not None and sdk >= 29:
            candidate, basis = _PORTAL_ANDROID10, f"a Meta Portal on SDK {sdk}"
        else:
            basis = "a Meta Portal on an SDK no Portal recipe covers"
    elif is_tv:
        candidate, basis = _ANDROID_TV, "an Android TV device (tv characteristic)"
    else:
        basis = "no existing recipe family matches this device"
    return {
        "candidate": candidate,
        "state": "proposed",
        "new_recipe_needed": candidate is None,
        "basis": basis,
    }


def build_support_request(report: dict, *, ksm_version: str) -> dict:
    """KSM-BEHAVE-164: the proposal for one capability report."""
    platform = report["facts"]["platform"]
    catalog = report["catalog"]
    model_key = catalog.get("model_key")
    if model_key is None:
        kind = KIND_NEW_DEVICE
    elif not catalog.get("executable_recipe"):
        kind = KIND_NEW_RECIPE
    elif catalog.get("support_state") in _UNQUALIFIED_BUILD:
        kind = KIND_NEW_BUILD
    else:
        kind = KIND_SUPPORTED

    if kind in (KIND_NEW_DEVICE, KIND_NEW_RECIPE):
        recipe = _candidate_recipe(platform)
    else:
        recipe = {
            "current": catalog.get("recipe_key"),
            "support_state": catalog.get("support_state"),
            "reason": catalog.get("reason"),
        }
    return {
        "kind": kind,
        "existing_model_key": model_key,
        "device_model": _device_model_draft(platform) if kind == KIND_NEW_DEVICE else None,
        "recipe": recipe,
        "ksm_version": ksm_version,
        "report": report,
    }


def issue_url(request: dict, issue_tracker: str) -> str:
    """KSM-BEHAVE-164: a new-issue URL pre-filling the device-support form."""
    label = device_label(request)
    proposal = {key: value for key, value in request.items() if key != "report"}

    def build(proposal: dict, report: dict | None) -> str:
        fields = {
            "template": ISSUE_FORM,
            "title": f"Device support: {label}",
            "device": label,
            "proposal": json.dumps(proposal, indent=2),
            "ksm_version": request["ksm_version"],
        }
        if report is not None:
            fields["report"] = json.dumps(report, indent=2)
        return f"{issue_tracker.rstrip('/')}/new?{urlencode(fields, quote_via=quote)}"

    url = build(proposal, request["report"])
    if len(url) <= MAX_URL_LENGTH:
        return url
    return build({**proposal, "attach": ATTACH_NOTE}, None)


def remember(hass: HomeAssistant, device_id: str, request: dict) -> None:
    hass.data.setdefault(_STORE_KEY, {})[device_id] = request


def remembered(hass: HomeAssistant, device_id: str) -> dict | None:
    """KSM-BEHAVE-166: the last request built for this device, for diagnostics."""
    return hass.data.get(_STORE_KEY, {}).get(device_id)


async def async_build_for_device(
    hass: HomeAssistant, entry: ConfigEntry | DeviceEntry
) -> tuple[dict, str]:
    """Collect the device's report over ADB and build its request and URL.

    Raises whatever the ADB connect raises; the client is closed either way.
    """
    client = AdbClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_KEY_PATH])
    try:
        await client.connect()
        report = await CapabilityReportCollector(client).collect()
    finally:
        await client.close()
    integration = await async_get_integration(hass, DOMAIN)
    request = build_support_request(report, ksm_version=str(integration.version))
    remember(hass, entry.entry_id, request)
    return request, issue_url(request, integration.issue_tracker or "")


async def async_url_for_client(hass: HomeAssistant, client: AdbClient) -> str:
    """KSM-BEHAVE-167: the request URL for a device onboarding refuses.

    It has no entry yet, so it reads through the flow's open client. A report
    that cannot be read still links the empty form rather than failing the flow.
    """
    integration = await async_get_integration(hass, DOMAIN)
    tracker = (integration.issue_tracker or "").rstrip("/")
    try:
        report = await CapabilityReportCollector(client).collect()
    except Exception as err:  # noqa: BLE001 -- the link is best-effort
        _LOGGER.debug("support request report failed: %s", type(err).__name__)
        return f"{tracker}/new?template={ISSUE_FORM}"
    return issue_url(build_support_request(report, ksm_version=str(integration.version)), tracker)
