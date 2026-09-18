"""Constants for Kiosk Satellite Manager."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "kiosk_satellite_manager"

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_KEY_PATH: Final = "key_path"
CONF_DEVICE_PROFILE: Final = "device_profile"

DEFAULT_ADB_PORT: Final = 5555
HEALTH_PORT: Final = 2324
HEALTH_TIMEOUT_S: Final = 5
HEALTH_SCAN_INTERVAL_MIN: Final = 5

KS_PACKAGE: Final = "me.jxl.kiosk_satellite"
KS_MAIN_ACTIVITY: Final = f"{KS_PACKAGE}/.MainActivity"
KS_APK_REMOTE_PATH: Final = "/data/local/tmp/kiosk-satellite.apk"
KS_GITHUB_REPO: Final = "jxlarrea/kiosk-satellite"

PLATFORMS: Final = ["button", "sensor"]

CONNECT_RETRY_ATTEMPTS: Final = 6
CONNECT_RETRY_DELAY_S: Final = 5
ADB_AUTH_TIMEOUT_S: Final = 5
ADB_CONNECT_TIMEOUT_S: Final = 10
