"""KSM-TEST-300 (#118): KSM is developed on OneDev but distributed through HACS
from the public GitHub repo, so everything a HACS user reads must point at
GitHub. Home Assistant shows manifest.json's documentation and issue_tracker
links on the integration page; a OneDev URL there is unreachable for anyone
but the developer. The GitHub workflow must run both validators HACS and
Home Assistant apply to a custom integration."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "custom_components" / "kiosk_satellite_manager" / "manifest.json"
GITHUB = "https://github.com/cfoxga/kiosk-satellite-manager"
SHIPPED_DOCS = ["README.md", "CONTRIBUTING.md", "hacs.json", "docs/supported-devices.md",
                "docs/install-recipes.md", "custom_components/kiosk_satellite_manager/manifest.json"]


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
