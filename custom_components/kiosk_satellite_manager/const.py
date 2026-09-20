"""Constants for Kiosk Satellite Manager."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "kiosk_satellite_manager"

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_KEY_PATH: Final = "key_path"
# KSM-BEHAVE-051 (issue #20): stores an exact `device_models.DeviceModel.model_key`,
# never a fallback classification and never a recipe key. The stored *key name*
# stays "device_profile" so entries created before the catalog keep resolving --
# the model keys were carried over 1:1 from the old profile keys.
CONF_DEVICE_PROFILE: Final = "device_profile"
# KSM-BEHAVE-009: entry-creation-time Name/Area; KSM-BEHAVE-010/011 read
# CONF_NAME/CONF_PASSWORD back out post-install to sync the device.
CONF_NAME: Final = "name"
CONF_AREA_ID: Final = "area_id"
CONF_PASSWORD: Final = "password"
CONF_TOKEN_MODE: Final = "token_mode"
TOKEN_MODE_AUTO: Final = "auto"
TOKEN_MODE_REUSE: Final = "reuse"
TOKEN_MODE_MANUAL: Final = "manual"
CONF_HA_TOKEN: Final = "ha_token"
CONF_HA_REFRESH_TOKEN_ID: Final = "ha_refresh_token_id"
CONF_HA_TOKEN_OWNED: Final = "ha_token_owned"
CONF_REUSE_ENTRY_ID: Final = "reuse_entry_id"
CONF_HOME_LAUNCHER: Final = "home_launcher"

# KSM-BEHAVE-021: an already-installed device is detected during the flow and
# the user chooses whether to keep the existing install or replace it.
CONF_EXISTING_INSTALL_ACTION: Final = "existing_install_action"
EXISTING_INSTALL_REUSE: Final = "reuse"
EXISTING_INSTALL_REINSTALL: Final = "reinstall"

DEFAULT_ADB_PORT: Final = 5555
HEALTH_PORT: Final = 2324
HEALTH_TIMEOUT_S: Final = 5
HEALTH_SCAN_INTERVAL_MIN: Final = 5

KS_PACKAGE: Final = "me.jxl.kiosk_satellite"
KS_MAIN_ACTIVITY: Final = f"{KS_PACKAGE}/.MainActivity"
KS_APK_REMOTE_PATH: Final = "/data/local/tmp/kiosk-satellite.apk"
KS_GITHUB_REPO: Final = "jxlarrea/kiosk-satellite"

PLATFORMS: Final = ["button", "sensor"]

# KSM-BEHAVE-018: Allow up to 120s (24 * 5s) for the user to tap "Allow USB debugging?"
# on a new or factory-reset device screen during the initial pairing attempt.
CONNECT_RETRY_ATTEMPTS: Final = 24
CONNECT_RETRY_DELAY_S: Final = 5
ADB_AUTH_TIMEOUT_S: Final = 5
ADB_CONNECT_TIMEOUT_S: Final = 10


# KSM-BEHAVE-007: after install+launch, poll the coordinator a few times
# instead of waiting for the next 5-minute cycle -- bounded so a device that
# never comes up (e.g. launch failed) doesn't poll forever.
INSTALL_LAUNCH_POLL_ATTEMPTS: Final = 10
INSTALL_LAUNCH_POLL_DELAY_S: Final = 2

# KSM-BEHAVE-010/011: after am start, the device's own web UI (:2324) needs
# a moment to come up before api/setup/status is reachable -- bounded so a
# device whose web server never comes up doesn't block forever.
SYNC_STATUS_POLL_ATTEMPTS: Final = 15
SYNC_STATUS_POLL_DELAY_S: Final = 2

# KSM-BEHAVE-011: matches the order of magnitude of HA's own "Long-Lived
# Access Tokens" profile-page feature (auth/long_lived_access_token, default
# lifespan shown in its docs example is 365 days; the profile-page UI's own
# default is multi-year) -- long enough not to need rotation for a kiosk's
# operational life.
HA_TOKEN_LIFESPAN_DAYS: Final = 3650
