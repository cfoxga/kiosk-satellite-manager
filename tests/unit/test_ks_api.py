"""Unit tests for the GitHub-releases APK asset picker (KSM-BEHAVE-001)."""
import pytest

from custom_components.kiosk_satellite_manager.ks_api import (
    ApkAssetNotFound,
    select_apk_asset,
)

# live-verified shape from the real jxlarrea/kiosk-satellite releases API (tag 2026.9.61)
_ASSETS = [
    ("kiosk-satellite-2026.9.61.apk", "https://example.invalid/kiosk-satellite-2026.9.61.apk"),
    ("kiosk-satellite-2026.9.61.arm64-v8a.apk", "https://example.invalid/arm64-v8a.apk"),
    ("kiosk-satellite-2026.9.61.armeabi-v7a.apk", "https://example.invalid/armeabi-v7a.apk"),
    ("kiosk-satellite-2026.9.61.x86_64.apk", "https://example.invalid/x86_64.apk"),
]


def test_picks_matching_abi_split():
    assert select_apk_asset(_ASSETS, "armeabi-v7a") == "https://example.invalid/armeabi-v7a.apk"


def test_falls_back_to_universal_build_for_unmatched_abi():
    assert (
        select_apk_asset(_ASSETS, "mips")
        == "https://example.invalid/kiosk-satellite-2026.9.61.apk"
    )


def test_empty_abi_falls_back_to_universal_build():
    assert (
        select_apk_asset(_ASSETS, "")
        == "https://example.invalid/kiosk-satellite-2026.9.61.apk"
    )


def test_raises_when_no_apk_assets():
    with pytest.raises(ApkAssetNotFound):
        select_apk_asset([], "arm64-v8a")
