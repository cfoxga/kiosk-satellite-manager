"""Device-type profiles: match rules for detected devices.

Data, not code (docs/SPEC/provisioning.md § Architecture: "Adding a device
type must be a data contribution, not a code change"). Kept as a plain Python
tuple here rather than a separate JSON asset because HACS ships the whole
custom_components/ tree as-is and this keeps the match rules type-checked;
splitting to JSON is a mechanical follow-up if profiles grow past a handful.

Match values below are getprop output, lower-cased before comparison
(Verified Finding 4). The onn/GTV values were confirmed live against a real
device this session; the Meta Portal manufacturer value was not -- no
ADB-enabled Portal was reachable -- and should be corrected against real
getprop output once one is (see docs/SPEC/provisioning.md § Open questions).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceProfile:
    """One matchable device type. Empty tuples mean "don't constrain on this"."""

    key: str
    name: str
    characteristics: tuple[str, ...] = ()
    manufacturer: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    min_sdk: int | None = None
    max_sdk: int | None = None
    device_name_command: str = "settings get global device_name"
    redundant_name_suffixes: tuple[str, ...] = (" Portal",)
    start_url_path: str = ""
    home_launcher_supported: bool = True
    is_portal: bool = False

    def normalize_device_name(self, value: str) -> str:
        """Remove a model suffix that Android appends to a user-set label."""
        value = value.strip()
        for suffix in self.redundant_name_suffixes:
            if suffix and value.endswith(suffix):
                stripped = value[: -len(suffix)].rstrip()
                if stripped:
                    return stripped
        return value

    def permissions_for_sdk(self, sdk: int) -> list[str]:
        """Permissions list adapted to what this Android API level actually defines."""
        perms = [
            "android.permission.RECORD_AUDIO",
            "android.permission.CAMERA",
            "android.permission.ACCESS_COARSE_LOCATION",
            "android.permission.ACCESS_FINE_LOCATION",
            "android.permission.READ_LOGS",
        ]
        if sdk >= 31:
            perms.extend(["android.permission.BLUETOOTH_SCAN", "android.permission.BLUETOOTH_CONNECT"])
        if sdk >= 33:
            perms.extend([
                "android.permission.POST_NOTIFICATIONS",
                "android.permission.READ_MEDIA_IMAGES",
                "android.permission.READ_MEDIA_VIDEO",
            ])
        if sdk <= 32:
            perms.append("android.permission.READ_EXTERNAL_STORAGE")
        if sdk <= 29:
            perms.append("android.permission.WRITE_EXTERNAL_STORAGE")
        if self.is_portal:
            perms.append("android.permission.WRITE_SECURE_SETTINGS")
        return perms

    def appops_for_sdk(self, sdk: int) -> list[str]:
        """AppOps grants for this device."""
        ops = ["SYSTEM_ALERT_WINDOW", "WRITE_SETTINGS", "GET_USAGE_STATS"]
        if sdk >= 30:
            ops.append("MANAGE_EXTERNAL_STORAGE")
        return ops


PORTAL_REDUNDANT_SUFFIXES: tuple[str, ...] = (
    " Portal",
    " PortalGo",
    " PortalMini",
    " Portal+",
    " PortalPlus",
    " PortalTV",
)

DEVICE_PROFILES: tuple[DeviceProfile, ...] = (
    # Specific Meta Portal models first (evaluated before generic meta_portal fallback)
    DeviceProfile(
        key="portal_go",
        name="Meta Portal Go",
        manufacturer=("facebook",),
        models=("portalgo",),
        device_name_command="settings get secure bluetooth_name",
        redundant_name_suffixes=PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        home_launcher_supported=True,
        is_portal=True,
    ),
    DeviceProfile(
        key="portal_mini",
        name="Meta Portal Mini",
        manufacturer=("facebook",),
        models=("portalmini",),
        device_name_command="settings get secure bluetooth_name",
        redundant_name_suffixes=PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        home_launcher_supported=True,
        is_portal=True,
    ),
    DeviceProfile(
        key="portal_gen2",
        name="Meta Portal (Gen 2)",
        manufacturer=("facebook",),
        models=("portal",),
        min_sdk=29,
        device_name_command="settings get secure bluetooth_name",
        redundant_name_suffixes=PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        home_launcher_supported=True,
        is_portal=True,
    ),
    DeviceProfile(
        key="portal_gen1",
        name="Meta Portal (Gen 1)",
        manufacturer=("facebook",),
        models=("portal",),
        max_sdk=28,
        device_name_command="settings get secure bluetooth_name",
        redundant_name_suffixes=PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        home_launcher_supported=True,
        is_portal=True,
    ),
    DeviceProfile(
        key="portal_plus_gen2",
        name="Meta Portal+ (Gen 2)",
        manufacturer=("facebook",),
        models=("portal+", "portalplus"),
        min_sdk=29,
        device_name_command="settings get secure bluetooth_name",
        redundant_name_suffixes=PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        home_launcher_supported=True,
        is_portal=True,
    ),
    DeviceProfile(
        key="portal_plus_gen1",
        name="Meta Portal+ (Gen 1)",
        manufacturer=("facebook",),
        models=("portal+", "portalplus"),
        max_sdk=28,
        device_name_command="settings get secure bluetooth_name",
        redundant_name_suffixes=PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        home_launcher_supported=True,
        is_portal=True,
    ),
    DeviceProfile(
        key="portal_tv",
        name="Meta Portal TV",
        manufacturer=("facebook",),
        models=("portaltv",),
        device_name_command="settings get secure bluetooth_name",
        redundant_name_suffixes=PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        home_launcher_supported=False,
        is_portal=True,
    ),
    # Generic Meta Portal fallback for any other Facebook device
    DeviceProfile(
        key="meta_portal",
        name="Meta Portal",
        manufacturer=("facebook",),
        device_name_command="settings get secure bluetooth_name",
        redundant_name_suffixes=PORTAL_REDUNDANT_SUFFIXES,
        start_url_path="/portal",
        home_launcher_supported=True,
        is_portal=True,
    ),

    DeviceProfile(
        key="gtv_stick",
        name="Google TV stick (onn/Chromecast-class)",
        characteristics=("tv,nosdcard", "tv"),
        manufacturer=("onn", "google"),
        device_name_command="settings get global device_name",
        start_url_path="",
        home_launcher_supported=False,
        is_portal=False,
    ),
)

UNKNOWN_PROFILE = DeviceProfile(key="unknown", name="Unknown device")


def get_profile(key: str | None) -> DeviceProfile:
    """Retrieve a device profile by its unique key, or UNKNOWN_PROFILE if not found."""
    if not key:
        return UNKNOWN_PROFILE
    for profile in DEVICE_PROFILES:
        if profile.key == key:
            return profile
    return UNKNOWN_PROFILE


def match_profile(
    characteristics: str = "",
    manufacturer: str = "",
    model: str = "",
    sdk: int = 0,
) -> DeviceProfile:
    """Pick the best-matching profile for a detected device's getprop output."""
    chars = characteristics.strip().lower()
    manuf = manufacturer.strip().lower()
    mod = model.strip().lower()

    for profile in DEVICE_PROFILES:
        if not profile.characteristics and not profile.manufacturer and not profile.models:
            continue
        if profile.characteristics and chars not in profile.characteristics:
            continue
        if profile.manufacturer and manuf not in profile.manufacturer:
            continue
        if profile.models and mod not in profile.models:
            continue
        if profile.min_sdk is not None and (sdk <= 0 or sdk < profile.min_sdk):
            continue
        if profile.max_sdk is not None and (sdk <= 0 or sdk > profile.max_sdk):
            continue
        return profile
    return UNKNOWN_PROFILE

