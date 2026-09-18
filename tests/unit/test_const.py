"""KSM-TEST-001: the integration domain constant is what the manifest declares."""
from custom_components.kiosk_satellite_manager.const import DOMAIN


def test_domain_matches_manifest():
    assert DOMAIN == "kiosk_satellite_manager"
