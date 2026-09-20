"""Read-only, sanitized Android capability reporting (KSM-BEHAVE-028)."""
from __future__ import annotations

import re
from typing import Any

from .const import KS_PACKAGE

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
        adb_identity, adb_identity_probe = await self._probe("id")
        accounts, accounts_probe = await self._probe("dumpsys account")
        users, users_probe = await self._probe("pm list users")
        owner, owner_probe = await self._probe("dpm get-device-owner")
        bootloader_locked, bootloader_locked_probe = await self._probe("getprop ro.boot.flash.locked")
        package_path, package_path_probe = await self._probe(f"pm path {KS_PACKAGE}")
        package_info, package_info_probe = await self._probe(f"dumpsys package {KS_PACKAGE}")

        sdk = int(sdk_raw) if sdk_raw and sdk_raw.isdecimal() else None
        version_match = _VERSION.search(package_info or "")
        adb_uid_match = _ADB_UID.search(adb_identity or "")
        installed = bool(package_path and package_path.startswith("package:"))
        report = {
            "schema_version": 1,
            "facts": {
                "platform": {"manufacturer": manufacturer, "model": model, "sdk": sdk},
                "management": {
                    "account_count": len(_ACCOUNT.findall(accounts or "")),
                    "device_owner": False if owner and "no device owner" in owner.lower() else True if owner else None,
                    "adb_uid": int(adb_uid_match.group(1)) if adb_uid_match else None,
                    "user_count": len(_USER.findall(users or "")),
                },
                "applications": {"kiosk_satellite": {
                    "installed": installed,
                    "version": version_match.group(1) if version_match else None,
                }},
                "oem": {"bootloader_locked": True if bootloader_locked == "1" else False if bootloader_locked == "0" else None},
            },
            "probes": {
                "manufacturer": manufacturer_probe, "model": model_probe, "sdk": sdk_probe,
                "adb_identity": adb_identity_probe, "accounts": accounts_probe, "users": users_probe,
                "device_owner": owner_probe, "bootloader_locked": bootloader_locked_probe,
                "kiosk_satellite_package": package_path_probe,
                "kiosk_satellite_details": package_info_probe,
            },
            "inferences": [
                *( ["android_sdk_unknown"] if sdk is None else [] ),
                *( ["kiosk_satellite_not_installed"] if not installed else [] ),
            ],
        }
        return report
