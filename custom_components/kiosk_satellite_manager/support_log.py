"""KSM-BEHAVE-159/160 (#133): an always-on, bounded record of KSM's
device-changing ADB runs, shipped in Home Assistant's "Download diagnostics".

Device Owner enrollment cannot be repeated without a factory reset, so the
record cannot wait for someone to turn debug logging on. It holds no secrets
by construction: nothing is copied in and scrubbed afterwards. A command
becomes a label made only of allowlisted source tokens, its output becomes a
fixed outcome class, and an error becomes a DeviceOwnerError code or an
exception class name (docs/SPEC/support-diagnostics.md).

`AdbClient.connect`/`shell` record into the run active in the current
context (a contextvar), so the flows only wrap a run around their work.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, KS_APK_REMOTE_PATH, KS_HOME_ACTIVITY, KS_MAIN_ACTIVITY, KS_PACKAGE
from .device_models import DEVICE_MODELS
from .device_owner import (
    ACCOUNT_CLEAR_PACKAGES, ANDROID9_CLEAR_PACKAGES, ANDROID9_DISABLE_PACKAGES, KS_ADMIN,
    META_SETUP_ACTIVITY, META_SETUP_PACKAGE, DeviceOwnerError,
)

_LOGGER = logging.getLogger(__name__)

STORE_KEY = f"{DOMAIN}.support_log"
STORE_VERSION = 1
DATA_KEY = f"{DOMAIN}_support_log"
MAX_RUNS = 20
MAX_STEPS = 150
UPTIME_COMMAND = "cat /proc/uptime"
_END_UPTIME_TIMEOUT_S = 5

KINDS = frozenset({"onboarding_install", "device_owner", "android9_cleanup", "meta_setup", "meta_watch"})
SOURCES = frozenset({"onboarding", "configure"})
_MODELS = frozenset(model.model_key for model in DEVICE_MODELS)

_PROGRAMS = frozenset({
    "am", "appops", "cat", "cmd", "dpm", "dumpsys", "echo", "getprop", "input", "ls", "monkey",
    "pm", "rm", "settings", "svc", "timeout", "wm",
})
_WORDS = frozenset({
    # subcommands
    "account", "activities", "activity", "clear", "delete", "deviceidle", "device_policy",
    "disable-user", "enable", "force-stop", "get", "get-current-user", "get-device-owner",
    "global", "grant", "install", "install-existing", "list", "notification", "package",
    "packages", "path", "put", "remove-active-admin", "resolve-activity", "secure",
    "set", "set-active-admin", "set-device-owner", "set-home-activity", "start", "system",
    "uninstall", "users", "whitelist",
    # flags and fixed arguments
    "--brief", "--user", "-a", "-c", "-d", "-e", "-g", "-k", "-n", "-r", "-W", "0",
    "android.intent.action.MAIN", "android.intent.category.HOME", "/proc/uptime",
    # global settings install recipes change (KSM-BEHAVE-152/184)
    "private_dns_mode", "package_verifier_enable",
    # KSM's own package constants
    KS_PACKAGE, KS_ADMIN, KS_MAIN_ACTIVITY, KS_HOME_ACTIVITY, KS_APK_REMOTE_PATH,
    META_SETUP_PACKAGE, META_SETUP_ACTIVITY,
    *ANDROID9_CLEAR_PACKAGES, *ANDROID9_DISABLE_PACKAGES,
    *(pkg for pkgs in ACCOUNT_CLEAR_PACKAGES.values() for pkg in pkgs),
})
_FAILURE_RE = re.compile(r"Failure \[([^\]]*)\]")
_FAILURE_CODE_RE = re.compile(r"[A-Z0-9_]{1,64}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,40}")
_NOTE_RE = re.compile(r"[a-z][a-z0-9_]{0,30}(:[A-Za-z][A-Za-z0-9_]{0,40})?")

_CURRENT: ContextVar["_Run | None"] = ContextVar(f"{DOMAIN}_support_run", default=None)


def command_label(command: str) -> str:
    """The command's allowlisted prefix; `…` marks where the rest was cut."""
    tokens = command.split() if isinstance(command, str) else []
    if not tokens:
        return "other"
    if tokens[0] not in _PROGRAMS:
        return "other …" if len(tokens) > 1 else "other"
    kept = [tokens[0]]
    for token in tokens[1:]:
        if token not in _WORDS:
            kept.append("…")
            break
        kept.append(token)
    return " ".join(kept)


def outcome_class(output: Any) -> str:
    """A fixed class for a command's output; the output is never kept."""
    if output is None:
        return "empty"
    if not isinstance(output, str):
        return "output"
    text = output.strip()
    if not text:
        return "empty"
    if "Failure" in text:
        match = _FAILURE_RE.search(text)
        if match and _FAILURE_CODE_RE.fullmatch(match.group(1)):
            return f"failure:{match.group(1)}"
        return "failure"
    if text.startswith("Success"):
        return "success"
    if "Exception" in text:
        return "exception"
    if text.startswith("Error") or "Error:" in text:
        return "error"
    return "output"


def error_code(err: BaseException) -> str:
    """A DeviceOwnerError code, else the exception class name -- never a message."""
    code = getattr(err, "code", None)
    if isinstance(err, DeviceOwnerError) and isinstance(code, str) and _CODE_RE.fullmatch(code):
        return code
    return type(err).__name__


def model_label(model_key: Any) -> str:
    return model_key if model_key in _MODELS else "unlisted"


def note_label(text: Any) -> str:
    return text if isinstance(text, str) and _NOTE_RE.fullmatch(text) else "invalid"


class _Run:
    def __init__(self, kind: str, source: str | None, model_key: str | None) -> None:
        self.started = time.monotonic()
        self.closed = False
        self.data: dict[str, Any] = {
            "kind": kind if kind in KINDS else "other",
            "source": source if source in SOURCES else None,
            "model": model_label(model_key),
            "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": 0.0,
            "result": "ok",
            "steps": [],
            "steps_truncated": 0,
            "uptime_before_s": None,
            "uptime_after_s": None,
            "rebooted": None,
        }

    def add(self, step: dict) -> None:
        if self.closed:
            return
        if len(self.data["steps"]) >= MAX_STEPS:
            self.data["steps_truncated"] += 1
            return
        self.data["steps"].append(step)

    def finish(self, result: str, uptime_after: int | None) -> dict:
        self.closed = True
        data = self.data
        data["duration_s"] = round(time.monotonic() - self.started, 1)
        data["result"] = result
        data["uptime_after_s"] = uptime_after
        before = data["uptime_before_s"]
        if before is not None and uptime_after is not None:
            data["rebooted"] = uptime_after < before
        return data


def parse_uptime(output: Any) -> int | None:
    try:
        return int(float(str(output).split()[0]))
    except (ValueError, IndexError):
        return None


def active() -> bool:
    run = _CURRENT.get()
    return run is not None and not run.closed


def record_command(command: str, outcome: str, ms: int, raised: str | None = None) -> None:
    """One ADB command in the current run (no-op outside a run)."""
    run = _CURRENT.get()
    if run is None or run.closed:
        return
    label = command_label(command)
    step: dict[str, Any] = {"cmd": label, "ms": int(ms)}
    if raised is None:
        step["out"] = outcome
    else:
        step["raised"] = raised
    _LOGGER.debug("ADB step %s -> %s (%d ms)", label, raised or outcome, ms)
    run.add(step)


def record_connect(raised: str | None, ms: int) -> None:
    run = _CURRENT.get()
    if run is None or run.closed:
        return
    step: dict[str, Any] = {"cmd": "connect", "ms": int(ms)}
    if raised is None:
        step["out"] = "ok"
    else:
        step["raised"] = raised
    run.add(step)


def needs_uptime() -> bool:
    """True when the current run has no start uptime yet."""
    run = _CURRENT.get()
    return run is not None and not run.closed and run.data["uptime_before_s"] is None


def record_uptime(seconds: int | None) -> None:
    run = _CURRENT.get()
    if run is not None and not run.closed and run.data["uptime_before_s"] is None:
        run.data["uptime_before_s"] = seconds


def detach() -> None:
    """Stop recording into an inherited run (call first in a background task)."""
    _CURRENT.set(None)


def note(text: str) -> None:
    run = _CURRENT.get()
    if run is not None:
        run.add({"note": note_label(text)})


async def async_load(hass: HomeAssistant) -> list[dict]:
    """The record, loaded from KSM's store once per HA run."""
    state = hass.data.get(DATA_KEY)
    if state is None:
        store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        runs: list[dict] = []
        try:
            saved = await store.async_load()
            if isinstance(saved, dict) and isinstance(saved.get("runs"), list):
                runs = [run for run in saved["runs"] if isinstance(run, dict)][-MAX_RUNS:]
        except Exception as err:  # noqa: BLE001 -- a bad record never blocks a run
            _LOGGER.debug("Support record unreadable: %s", type(err).__name__)
        state = hass.data.setdefault(DATA_KEY, {"store": store, "runs": runs})
    return state["runs"]


def runs(hass: HomeAssistant) -> list[dict]:
    """The runs kept in memory (empty before the first load)."""
    return list(hass.data.get(DATA_KEY, {}).get("runs", []))


async def _async_append(hass: HomeAssistant, run: dict) -> None:
    try:
        kept = await async_load(hass)
        kept.append(run)
        del kept[:-MAX_RUNS]
        await hass.data[DATA_KEY]["store"].async_save({"runs": kept})
    except Exception as err:  # noqa: BLE001 -- recording never changes an outcome
        _LOGGER.debug("Support record not saved: %s", type(err).__name__)


async def _end_uptime(client: Any) -> int | None:
    read = getattr(client, "uptime_s", None)
    if read is None:
        return None
    try:
        async with asyncio.timeout(_END_UPTIME_TIMEOUT_S):
            return await read()
    except Exception:  # noqa: BLE001 -- unreadable means unknown
        return None


@contextlib.asynccontextmanager
async def async_run(
    hass: HomeAssistant, kind: str, *, model_key: str | None = None,
    source: str | None = None, client: Any = None,
) -> AsyncIterator[None]:
    """Record one device-changing run. Exceptions propagate unchanged."""
    await async_load(hass)
    run = _Run(kind, source, model_key)
    token = _CURRENT.set(run)
    result = "ok"
    try:
        yield
    except BaseException as err:
        result = error_code(err)
        raise
    finally:
        uptime_after = None
        if run.data["uptime_before_s"] is not None and client is not None:
            uptime_after = await _end_uptime(client)
        _CURRENT.reset(token)
        await _async_append(hass, run.finish(result, uptime_after))


async def async_event(
    hass: HomeAssistant, kind: str, *, model_key: str | None, result: str,
    uptime_s: int | None = None,
) -> None:
    """A run with no steps, e.g. the Meta-login watch ending."""
    run = _Run(kind, None, model_key)
    run.closed = True
    await _async_append(hass, run.finish(note_label(result), uptime_s))
