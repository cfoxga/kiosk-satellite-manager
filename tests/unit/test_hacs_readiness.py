"""KSM-TEST-300 (#118): KSM is developed on OneDev but distributed through HACS
from the public GitHub repo, so everything a HACS user reads must point at
GitHub. Home Assistant shows manifest.json's documentation and issue_tracker
links on the integration page; a OneDev URL there is unreachable for anyone
but the developer. The GitHub workflow must run both validators HACS and
Home Assistant apply to a custom integration."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "custom_components" / "kiosk_satellite_manager" / "manifest.json"
GITHUB = "https://github.com/cfoxga/kiosk-satellite-manager"
SHIPPED_DOCS = ["README.md", "CONTRIBUTING.md", "hacs.json", "docs/supported-devices.md",
                "docs/install-recipes.md", "docs/device-owner.md",
                "custom_components/kiosk_satellite_manager/manifest.json"]


def test_manifest_links_point_at_github():
    """[KSM-TEST-300] manifest documentation and issue_tracker are GitHub URLs."""
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["documentation"].startswith(f"{GITHUB}/")
    assert manifest["issue_tracker"] == f"{GITHUB}/issues"


def test_shipped_docs_have_no_onedev_links():
    """[KSM-TEST-300] no user-facing file links to the private OneDev server."""
    offenders = [name for name in SHIPPED_DOCS if "onedev.cfoxga.com" in (ROOT / name).read_text()]
    assert offenders == []


def test_readme_documents_hacs_install():
    """[KSM-TEST-300] README gives the HACS custom-repository install."""
    readme = (ROOT / "README.md").read_text()
    assert "Custom repositories" in readme
    assert GITHUB in readme


def test_validation_workflow_runs_hassfest_and_hacs():
    """[KSM-TEST-300] the GitHub workflow runs hassfest and the HACS action for an integration."""
    workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text()
    assert "home-assistant/actions/hassfest@" in workflow
    assert "hacs/action@" in workflow
    assert "category: integration" in workflow


def _h2_headings(readme: str) -> list[str]:
    return [line[3:].strip() for line in readme.splitlines() if line.startswith("## ")]


def test_readme_follows_hacs_layout():
    """[KSM-TEST-300] (#122) Installation is the first section, with the My Home Assistant
    "open in HACS" and "add integration" buttons, as HACS integration READMEs conventionally do."""
    readme = (ROOT / "README.md").read_text()
    assert _h2_headings(readme)[0] == "Installation"
    assert (
        "https://my.home-assistant.io/redirect/hacs_repository/"
        "?owner=cfoxga&repository=kiosk-satellite-manager&category=integration" in readme
    )
    assert "https://my.home-assistant.io/badges/hacs_repository.svg" in readme
    assert "https://my.home-assistant.io/redirect/config_flow_start/?domain=kiosk_satellite_manager" in readme
    assert "https://my.home-assistant.io/badges/config_flow_start.svg" in readme


def test_manifest_and_strings_pass_hassfest_rules():
    """[KSM-TEST-307] (#124) hassfest rules the GitHub Validate run enforces: requirements give a
    minimum version, never an `==` pin (HA core also depends on adb-shell), and every config
    subentry type has `entry_type` plus the required `initiate_flow.user` label."""
    pkg = ROOT / "custom_components" / "kiosk_satellite_manager"
    manifest = json.loads((pkg / "manifest.json").read_text())
    assert manifest["requirements"]
    assert all("==" not in req for req in manifest["requirements"])
    for name in ("strings.json", "translations/en.json"):
        subentries = json.loads((pkg / name).read_text())["config_subentries"]
        for subentry in subentries.values():
            assert subentry["entry_type"]
            assert subentry["initiate_flow"]["user"]


def test_device_owner_guide_is_linked_and_timeless():
    """[KSM-TEST-308] (#128) the Device Owner guide is linked from the README and carries
    no issue references or calendar dates, so it reads the same to anyone outside the tracker."""
    guide = (ROOT / "docs" / "device-owner.md").read_text()
    assert "](docs/device-owner.md)" in (ROOT / "README.md").read_text()
    assert re.findall(r"(?<![\w&/])#\d+\b", guide) == []
    assert re.findall(r"\b\d{4}-\d{2}-\d{2}\b", guide) == []
