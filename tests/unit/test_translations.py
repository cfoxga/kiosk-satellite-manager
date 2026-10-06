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


def _options_flow_step_ids() -> set[str]:
    """Every literal `step_id=` shown by KioskSatelliteManagerOptionsFlow,
    which KioskSatelliteDeviceSubentryFlow inherits."""
    import ast

    tree = ast.parse((PKG / "config_flow.py").read_text())
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "KioskSatelliteManagerOptionsFlow"
    )
    return {
        kw.value.value
        for node in ast.walk(cls)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "step_id" and isinstance(kw.value, ast.Constant)
    }


def test_every_inherited_device_step_has_subentry_text():
    """[KSM-TEST-286] The device subentry Configure flow inherits its steps
    from the options flow, and HA reads their text from
    config_subentries.device.step -- a step missing there (android9_cleanup)
    renders with no explanation."""
    strings = json.loads((PKG / "strings.json").read_text())
    subentry_steps = set(strings["config_subentries"]["device"]["step"])
    # init (the manager menu), settings and certificates are manager-entry forms
    inherited = _options_flow_step_ids() - {"init", "settings", "certificates"}
    assert "android9_cleanup" in inherited
    assert inherited <= subentry_steps, sorted(inherited - subentry_steps)
    step = strings["config_subentries"]["device"]["step"]["android9_cleanup"]
    assert step["title"] and step["description"]
