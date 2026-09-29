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
CONF_ENABLE_DEVICE_OWNER: Final = "enable_device_owner"
CONF_ENABLE_ESPHOME: Final = "enable_esphome"
# KSM-BEHAVE-073: per-entry opt-in, stored in entry.options, default off.
CONF_AUTO_UPDATE: Final = "auto_update"
# KSM-BEHAVE-080: manager-entry option, default off; while on, every device's
# update entity auto-updates as if its own CONF_AUTO_UPDATE were on.
CONF_AUTO_UPDATE_ALL: Final = "auto_update_all"
# KSM-BEHAVE-110: manager-entry option, default off; a device added while it
# is on gets CONF_ESPHOME_ENABLE_PENDING in its entry data, and its first
# successful setup turns Kiosk Satellite's ESPHome server on and drops the flag.
CONF_ESPHOME_NEW_DEVICES: Final = "esphome_new_devices"
# KSM-BEHAVE-133 (#95): manager option, default off; hides confirmed followers' ESPHome update entity.
CONF_HIDE_FOLLOWER_UPDATES: Final = "hide_follower_updates"
# KSM-BEHAVE-114 (#72): manager option, "latest" or a cached version.
CONF_TARGET_VERSION: Final = "target_version"
TARGET_VERSION_LATEST: Final = "latest"
CONF_ESPHOME_ENABLE_PENDING: Final = "esphome_enable_pending"
CONF_ENTRY_TYPE: Final = "entry_type"
ENTRY_TYPE_MANAGER: Final = "manager"
ENTRY_TYPE_UNMANAGED: Final = "unmanaged"
ENTRY_TYPE_FLEET: Final = "fleet"
MANAGER_UNIQUE_ID: Final = "ksm_manager"
UNMANAGED_UNIQUE_ID: Final = "ksm_unmanaged"
CONF_HA_URL: Final = "ha_url"
CONF_TLS_SPKI: Final = "tls_spki_sha256"
CONF_ONBOARDING_MODE: Final = "onboarding_mode"
ONBOARDING_REVIEW: Final = "review"
ONBOARDING_AUTOMATIC: Final = "automatic"
MANAGER_UPDATE_RUNNING_KEY: Final = f"{DOMAIN}_update_all_running"
MANAGER_ENTRY_KEY: Final = f"{DOMAIN}_manager_entry"
# KSM-BEHAVE-102: the in-process rename callable other integrations use when
# no user context exists (a registry event); never a service or WS command.
RENAME_API_KEY: Final = f"{DOMAIN}_rename"

# KSM-BEHAVE-021: an already-installed device is detected during the flow and
# the user chooses whether to keep the existing install or replace it.
CONF_EXISTING_INSTALL_ACTION: Final = "existing_install_action"
EXISTING_INSTALL_REUSE: Final = "reuse"
EXISTING_INSTALL_REINSTALL: Final = "reinstall"

DEFAULT_ADB_PORT: Final = 5555
HEALTH_PORT: Final = 2324
HEALTH_TIMEOUT_S: Final = 5
# KSM-BEHAVE-107/108 (#70): a ~100-200 MB APK from GitHub to HA, then from HA
# to a kiosk over Wi-Fi. Generous but bounded -- a stalled transfer fails.
APK_DOWNLOAD_TIMEOUT_S: Final = 600
APK_UPLOAD_TIMEOUT_S: Final = 900
# KSM-BEHAVE-109: rule 3 of the cache retention -- the newest N versions.
APK_CACHE_KEEP_LATEST: Final = 3
# KSM-BEHAVE-116 (#74): the Install version list offers at least this many versions.
INSTALL_VERSION_CHOICES_MIN: Final = 5
HEALTH_SCAN_INTERVAL_MIN: Final = 5

KS_PACKAGE: Final = "me.jxl.kiosk_satellite"
KS_MAIN_ACTIVITY: Final = f"{KS_PACKAGE}/.MainActivity"
KS_APK_REMOTE_PATH: Final = "/data/local/tmp/kiosk-satellite.apk"
KS_GITHUB_REPO: Final = "jxlarrea/kiosk-satellite"

PLATFORMS: Final = ["binary_sensor", "button", "select", "sensor", "switch"]
MANAGER_PLATFORMS: Final = ["button", "sensor", "switch"]
# KSM-BEHAVE-080: sent when the manager's Auto-update all switch changes so
# every device's update entity re-evaluates its auto-update rules.
SIGNAL_AUTO_UPDATE_ALL: Final = f"{DOMAIN}_auto_update_all_changed"
SIGNAL_MANAGER_OPTIONS_UPDATED: Final = f"{DOMAIN}_manager_options_updated"

# KSM-BEHAVE-071: one release check per HA instance, kept beside (not inside)
# hass.data[DOMAIN], which maps entry_id -> health coordinator and is emptied
# to decide when the last entry has unloaded.
RELEASE_COORDINATOR_KEY: Final = f"{DOMAIN}_release"
RELEASE_CHECK_INTERVAL_MIN: Final = 15

# KSM-BEHAVE-018: Allow up to 120s (24 * 5s) for the user to tap "Allow USB debugging?"
# on a new or factory-reset device screen during the initial pairing attempt.
CONNECT_RETRY_ATTEMPTS: Final = 24
CONNECT_RETRY_DELAY_S: Final = 5
ADB_AUTH_TIMEOUT_S: Final = 5
ADB_CONNECT_TIMEOUT_S: Final = 10
# KSM-BEHAVE-079: ADB-enabled binary sensor -- bare TCP probe, own poll.
ADB_PROBE_TIMEOUT_S: Final = 3
ADB_PROBE_INTERVAL_MIN: Final = 5
PERMISSIONS_POLL_INTERVAL_MIN: Final = 15
PERMISSIONS_MONITOR_KEY: Final = f"{DOMAIN}_permissions"


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

# KSM-BEHAVE-066: automatic KSM credentials are rotated by Install/Reinstall
# and expire in 90 days if a kiosk is retired or lost before that lifecycle
# action can run. Operator-supplied credentials remain untouched.
HA_TOKEN_LIFESPAN_DAYS: Final = 90

# KSM-BEHAVE-082: bounded poll window after installUpdate for Kiosk
# Satellite's own self-update to either land (/api/health appVersion == V)
# or resolve to "awaiting confirmation" (Android's on-screen install
# prompt) -- 120 * 5s = 10 minutes, matching the design doc's default.
SELF_UPDATE_POLL_ATTEMPTS: Final = 120
SELF_UPDATE_POLL_DELAY_S: Final = 5

# KSM-BEHAVE-104/105: manager-entry options for device configuration backups.
# Period 0 disables scheduled backups; keep is the per-device retained count.
CONF_BACKUP_INTERVAL_HOURS: Final = "backup_interval_hours"
CONF_BACKUP_KEEP: Final = "backup_keep"
DEFAULT_BACKUP_INTERVAL_HOURS: Final = 24
DEFAULT_BACKUP_KEEP: Final = 10
BACKUP_CHECK_INTERVAL_MIN: Final = 15
SIGNAL_BACKUPS_CHANGED: Final = f"{DOMAIN}_backups_changed"
