"""KSM-TEST-323/324 (kiosk-satellite-manager#133): the Device Owner texts say
Meta's setup screen is not a reboot or reset (KSM-BEHAVE-162), and KSM's
"Enable debug logging" does not switch on adb_shell's transport log."""
import json
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "custom_components" / "kiosk_satellite_manager"
NOT_A_REBOOT = re.compile(r"\b(did|does|will) not reboot or reset\b")


def _strings() -> dict:
    return json.loads((PKG / "strings.json").read_text())


def test_device_owner_texts_say_no_reboot():
    """[KSM-TEST-323] Both Device Owner confirmations and both "setup screen
    is showing" results say the Portal does not reboot or reset."""
    strings = _strings()
    texts = {
        "onboard_device_owner": strings["config"]["step"]["onboard_device_owner"]["description"],
        "device_owner": strings["options"]["step"]["device_owner"]["description"],
        "device_owner_enabled_meta_setup": strings["options"]["abort"]["device_owner_enabled_meta_setup"],
        "meta_setup_started": strings["options"]["abort"]["meta_setup_started"],
    }
    missing = [name for name, text in texts.items() if not NOT_A_REBOOT.search(text)]
    assert missing == []


def test_en_translation_still_matches_strings():
    """[KSM-TEST-323] The shipped en.json carries the same wording."""
    en = json.loads((PKG / "translations" / "en.json").read_text())
    assert en == _strings()


def test_manifest_has_no_transport_logger():
    """[KSM-TEST-324] No `loggers`: debug logging must not turn on adb_shell's
    transport output, which can carry raw shell payloads."""
    manifest = json.loads((PKG / "manifest.json").read_text())
    assert "loggers" not in manifest
