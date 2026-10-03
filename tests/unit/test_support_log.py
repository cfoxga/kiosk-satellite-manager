"""KSM-TEST-318 (kiosk-satellite-manager#133): the support record's command
labels, outcome classes and error codes are built only from fixed source
vocabulary, so no secret, host, account or setting value can reach the
diagnostics file (KSM-BEHAVE-160)."""
from __future__ import annotations

import pytest

from custom_components.kiosk_satellite_manager import support_log
from custom_components.kiosk_satellite_manager.device_owner import (
    DeviceOwnerError, KS_ADMIN, META_SETUP_ACTIVITY,
)

META = "com.facebook.alohaservices.alohausers"
SECRET = "hunter2-password"


@pytest.mark.parametrize("command,label", [
    (f"pm uninstall -k --user 0 {META}", f"pm uninstall -k --user 0 {META}"),
    (f"dpm set-device-owner {KS_ADMIN}", f"dpm set-device-owner {KS_ADMIN}"),
    ("dumpsys account", "dumpsys account"),
    ("cat /proc/uptime", "cat /proc/uptime"),
    (f"am start -n {META_SETUP_ACTIVITY}", f"am start -n {META_SETUP_ACTIVITY}"),
])
def test_label_keeps_allowlisted_tokens(command, label):
    """[KSM-TEST-318] KSM's own commands keep their subcommands, flags and
    package constants, so the record shows what ran."""
    assert support_log.command_label(command) == label


@pytest.mark.parametrize("command,label,leaked", [
    (f"am start -n {META_SETUP_ACTIVITY} --es password {SECRET}",
     f"am start -n {META_SETUP_ACTIVITY} …", SECRET),
    (f"settings put secure private_dns_specifier {SECRET}.example.net",
     "settings put secure …", SECRET),
    ("timeout 2 ping -c 1 portal.home.example", "timeout …", "portal.home.example"),
    (f"curl -u admin:{SECRET} http://192.0.2.9", "other …", SECRET),
    (f"echo {SECRET} > /sdcard/x", "echo …", SECRET),
    (f"pm install -r -g /data/local/tmp/{SECRET}.apk", "pm install -r -g …", SECRET),
])
def test_label_drops_everything_after_first_unknown_token(command, label, leaked):
    """[KSM-TEST-318] Negative: an intent extra, a setting value, a host or an
    unknown first token never reaches the label."""
    out = support_log.command_label(command)
    assert out == label
    assert leaked not in out and "192.0.2.9" not in out


def test_label_of_unknown_first_token_is_other():
    """[KSM-TEST-318] An unknown program name is replaced, not echoed."""
    assert support_log.command_label("mysecretbinary") == "other"
    assert support_log.command_label("") == "other"


@pytest.mark.parametrize("output,cls", [
    ("", "empty"),
    ("  \n", "empty"),
    ("Success\n", "success"),
    ("Success: Device owner set to package me.jxl.kiosk_satellite", "success"),
    ("Failure [INSTALL_FAILED_VERSION_DOWNGRADE]", "failure:INSTALL_FAILED_VERSION_DOWNGRADE"),
    ("Failure [install failed person@example.com]", "failure"),
    ("Failure [lower_case]", "failure"),
    ("Error: Activity class {x} does not exist.", "error"),
    ("java.lang.IllegalStateException: Not allowed to set the device owner", "exception"),
    ("Account {name=person@example.com, type=com.facebook.aloha.sso}", "output"),
])
def test_outcome_class(output, cls):
    """[KSM-TEST-318] Output is reduced to a fixed class; only an upper-case
    pm Failure code survives, and never the output itself."""
    assert support_log.outcome_class(output) == cls


def test_outcome_class_of_non_text_is_output():
    """[KSM-TEST-318] A non-string shell result still classifies."""
    assert support_log.outcome_class(b"\x00\x01") == "output"
    assert support_log.outcome_class(None) == "empty"


@pytest.mark.parametrize("err,code", [
    (DeviceOwnerError("set_owner_failed", f"detail {SECRET}"), "set_owner_failed"),
    (DeviceOwnerError("Bad Code With Spaces", SECRET), "DeviceOwnerError"),
    (RuntimeError(f"connection to 192.0.2.9 lost {SECRET}"), "RuntimeError"),
    (TimeoutError(), "TimeoutError"),
])
def test_error_code_never_carries_a_message(err, code):
    """[KSM-TEST-318] An error code is a DeviceOwnerError code or a class
    name; exception messages never enter the record."""
    assert support_log.error_code(err) == code


@pytest.mark.parametrize("model,stored", [
    ("portal_mini", "portal_mini"),
    (None, "unlisted"),
    ("Chris's Kitchen Portal", "unlisted"),
])
def test_model_is_a_known_key_or_unlisted(model, stored):
    """[KSM-TEST-318] A model outside device_models is never stored as given."""
    assert support_log.model_label(model) == stored


@pytest.mark.parametrize("text,stored", [
    ("meta_setup:shown", "meta_setup:shown"),
    ("meta_setup:setup_not_shown", "meta_setup:setup_not_shown"),
    (f"meta_setup:{SECRET}@x", "invalid"),
])
def test_note_text_is_restricted(text, stored):
    """[KSM-TEST-318] A note outside the fixed shape is replaced."""
    assert support_log.note_label(text) == stored
