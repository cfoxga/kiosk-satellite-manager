"""KSM-TEST-299 (#117): the version Home Assistant shows for KSM comes from
manifest.json, and the release notes for it are the CHANGELOG section of the
same number. A release bump that misses either file ships a version whose notes
are elsewhere, or notes for a version nobody can install."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "custom_components" / "kiosk_satellite_manager" / "manifest.json"
CHANGELOG = ROOT / "CHANGELOG.md"


def _newest_release_heading() -> str:
    """The first `## X.Y.Z` heading, skipping `## Unreleased`."""
    for line in CHANGELOG.read_text().splitlines():
        match = re.fullmatch(r"## (\d+\.\d+\.\d+)", line.strip())
        if match:
            return match.group(1)
    raise AssertionError("CHANGELOG.md has no released `## X.Y.Z` heading")


def test_manifest_version_matches_newest_changelog_release():
    """[KSM-TEST-299] manifest.json version equals the newest released CHANGELOG heading."""
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["version"] == _newest_release_heading()
