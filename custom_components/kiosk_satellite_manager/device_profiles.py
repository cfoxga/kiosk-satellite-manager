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


DEVICE_PROFILES: tuple[DeviceProfile, ...] = (
    DeviceProfile(
        key="gtv_stick",
        name="Google TV stick (onn/Chromecast-class)",
        characteristics=("tv,nosdcard", "tv"),
        manufacturer=("onn", "google"),
    ),
    DeviceProfile(
        key="meta_portal",
        name="Meta Portal",
        manufacturer=("facebook",),
    ),
)

UNKNOWN_PROFILE = DeviceProfile(key="unknown", name="Unknown device")


def match_profile(characteristics: str, manufacturer: str) -> DeviceProfile:
    """Pick the best-matching profile for a detected device's getprop output."""
    chars = characteristics.strip().lower()
    manuf = manufacturer.strip().lower()
    for profile in DEVICE_PROFILES:
        if not profile.characteristics and not profile.manufacturer:
            continue
        if profile.characteristics and chars not in profile.characteristics:
            continue
        if profile.manufacturer and manuf not in profile.manufacturer:
            continue
        return profile
    return UNKNOWN_PROFILE
