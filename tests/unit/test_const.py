"""KSM-TEST-001: the integration domain constant is what the manifest declares."""
import custom_components.kiosk_satellite_manager.const as const
from custom_components.kiosk_satellite_manager.const import DOMAIN


def test_domain_matches_manifest():
    assert DOMAIN == "kiosk_satellite_manager"


def test_no_default_admin_password_constant():
    """KSM-TEST-006: the hardcoded 1newpass default is gone for good."""
    assert not hasattr(const, "DEFAULT_ADMIN_PASSWORD")


def test_existing_install_action_constants():
    """KSM-BEHAVE-021: the package-choice action values are stable."""
    assert const.CONF_EXISTING_INSTALL_ACTION == "existing_install_action"
    assert const.EXISTING_INSTALL_REUSE == "reuse"
    assert const.EXISTING_INSTALL_REINSTALL == "reinstall"

