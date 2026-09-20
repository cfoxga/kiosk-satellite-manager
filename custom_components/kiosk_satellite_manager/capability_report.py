"""Read-only, sanitized Android capability reporting (KSM-BEHAVE-028/036)."""
from __future__ import annotations

import re
from typing import Any

from .const import KS_PACKAGE
from .device_profiles import match_profile

_UNKNOWN = ("unknown command", "not found", "not supported")
_ACCOUNT = re.compile(r"Account\s*\{")
_VERSION = re.compile(r"\bversionName=([^\s]+)")
_ADB_UID = re.compile(r"\buid=(\d+)")
_USER = re.compile(r"UserInfo\{")


class CapabilityReportCollector:
    """Collect allowlisted read-only probes without retaining raw output."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _probe(self, command: str) -> tuple[str | None, dict[str, str]]:
        try:
            output = (await self._client.shell(command)).strip()
        except PermissionError as err:
            return None, {"status": "denied", "error": type(err).__name__}
        except Exception as err:  # device-specific ADB failures are evidence, not data
            return None, {"status": "error", "error": type(err).__name__}
        if not output or any(marker in output.lower() for marker in _UNKNOWN):
            return None, {"status": "unsupported"}
        return output, {"status": "ok"}

    async def collect(self) -> dict[str, Any]:
        manufacturer, manufacturer_probe = await self._probe("getprop ro.product.manufacturer")
        model, model_probe = await self._probe("getprop ro.product.model")
        sdk_raw, sdk_probe = await self._probe("getprop ro.build.version.sdk")
        characteristics, characteristics_probe = await self._probe("getprop ro.build.characteristics")
        codename, codename_probe = await self._probe("getprop ro.product.device")
        fingerprint, fingerprint_probe = await self._probe("getprop ro.build.fingerprint")
        adb_identity, adb_identity_probe = await self._probe("id")
        accounts, accounts_probe = await self._probe("dumpsys account")
        users, users_probe = await self._probe("pm list users")
        owner, owner_probe = await self._probe("dpm get-device-owner")
        bootloader_locked, bootloader_locked_probe = await self._probe("getprop ro.boot.flash.locked")
        # KSM-BEHAVE-039, cfoxga/kiosk-satellite-manager#15: `ro.test_harness`
        # is the legacy isRunningInTestHarness()/test-farm signal -- a build
        # can report it without ever having run `cmd testharness enable`, and
        # Test Harness Mode can set it as a side effect of the wipe. It is
        # NOT the same evidence as `persist.sys.test_harness` below; do not
        # conflate the two (docs/developer/android-support/test-harness-boundary.md).
        ro_test_harness, ro_test_harness_probe = await self._probe("getprop ro.test_harness")
        # KSM-BEHAVE-036, cfoxga/kiosk-satellite-manager#12 Phase 1: read-only
        # Test Harness detection -- never invokes `cmd testharness enable`.
        test_harness_active, test_harness_active_probe = await self._probe("getprop persist.sys.test_harness")
        test_harness_command, test_harness_command_probe = await self._probe("cmd testharness get-info")
        # KSM-OPEN-004: `locksettings get-disabled` is not yet live-confirmed
        # against Portal hardware -- treated the same as any other probe that
        # can legitimately come back "unsupported" on a given build.
        lockscreen_disabled, lockscreen_disabled_probe = await self._probe("locksettings get-disabled")
        package_path, package_path_probe = await self._probe(f"pm path {KS_PACKAGE}")
        package_info, package_info_probe = await self._probe(f"dumpsys package {KS_PACKAGE}")

        sdk = int(sdk_raw) if sdk_raw and sdk_raw.isdecimal() else None
        version_match = _VERSION.search(package_info or "")
        adb_uid_match = _ADB_UID.search(adb_identity or "")
        installed = bool(package_path and package_path.startswith("package:"))
        device_profile = match_profile(
            characteristics or "", manufacturer or "", model=model or "", sdk=sdk or 0
        )
        lockscreen_secure = (
            not (lockscreen_disabled.strip().lower() == "true")
            if lockscreen_disabled is not None
            else None
        )
        report = {
            "schema_version": 1,
            "facts": {
                "platform": {
                    "manufacturer": manufacturer, "model": model, "sdk": sdk,
                    "characteristics": characteristics, "codename": codename,
                    "fingerprint": fingerprint, "device_profile_key": device_profile.key,
                },
                "management": {
                    "account_count": len(_ACCOUNT.findall(accounts or "")),
                    "device_owner": False if owner and "no device owner" in owner.lower() else True if owner else None,
                    "adb_uid": int(adb_uid_match.group(1)) if adb_uid_match else None,
                    "user_count": len(_USER.findall(users or "")),
                    "lockscreen_secure": lockscreen_secure,
                },
                "applications": {"kiosk_satellite": {
                    "installed": installed,
                    "version": version_match.group(1) if version_match else None,
                }},
                "oem": {
                    "bootloader_locked": True if bootloader_locked == "1" else False if bootloader_locked == "0" else None,
                    "test_harness_mode": True if ro_test_harness == "1" else False if ro_test_harness == "0" else None,
                },
                "test_harness": {
                    "active": True if test_harness_active == "1" else False if test_harness_active == "0" else None,
                    "command_supported": test_harness_command is not None,
                },
            },
            "probes": {
                "manufacturer": manufacturer_probe, "model": model_probe, "sdk": sdk_probe,
                "characteristics": characteristics_probe, "codename": codename_probe,
                "fingerprint": fingerprint_probe,
                "adb_identity": adb_identity_probe, "accounts": accounts_probe, "users": users_probe,
                "device_owner": owner_probe, "bootloader_locked": bootloader_locked_probe,
                "test_harness_mode": ro_test_harness_probe,
                "test_harness_active": test_harness_active_probe,
                "test_harness_command": test_harness_command_probe,
                "lockscreen_disabled": lockscreen_disabled_probe,
                "kiosk_satellite_package": package_path_probe,
                "kiosk_satellite_details": package_info_probe,
            },
            "inferences": [
                *( ["android_sdk_unknown"] if sdk is None else [] ),
                *( ["kiosk_satellite_not_installed"] if not installed else [] ),
                *( ["device_profile_unmatched"] if device_profile.key == "unknown" else [] ),
            ],
        }
        return report
