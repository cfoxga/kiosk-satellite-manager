"""KSM-TEST-170 (#54): Home Assistant loads a custom integration's UI text
only from translations/<lang>.json, so the shipped en.json must match
strings.json exactly -- otherwise every KSM dialog renders without its
explanation, including the Device Owner confirmation."""
import json
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "custom_components" / "kiosk_satellite_manager"


def test_en_translation_matches_strings():
    """[KSM-TEST-170] en.json exists and equals strings.json."""
    strings = json.loads((PKG / "strings.json").read_text())
    en = json.loads((PKG / "translations" / "en.json").read_text())
    assert en == strings
    owner = strings["options"]["step"]["device_owner"]
    assert "{accounts}" in owner["description"]
