"""[KSM-TEST-403] Diagnostics redact the Certificates secrets and device keys (#200)."""
from custom_components.kiosk_satellite_manager import diagnostics
from custom_components.kiosk_satellite_manager.const import (
    ACME_DEVICE_FIELDS, CONF_ACME_ACCOUNT_KEY, CONF_ACME_ACCOUNT_URL, CONF_ACME_DNS_TOKEN,
    CONF_ACME_EMAIL,
)


def test_to_redact_covers_every_acme_secret():
    required = {CONF_ACME_DNS_TOKEN, CONF_ACME_ACCOUNT_KEY, CONF_ACME_ACCOUNT_URL,
                CONF_ACME_EMAIL, *ACME_DEVICE_FIELDS}
    assert required <= set(diagnostics.TO_REDACT)
