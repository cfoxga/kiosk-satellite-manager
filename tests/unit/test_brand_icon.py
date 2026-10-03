"""KSM-TEST-325 (#134): Home Assistant 2026.3+ serves a custom integration's
icon from custom_components/<domain>/brand/ through /api/brands/integration/
<domain>/<image>, only for the file names it allows. Without icon.png HA falls
back to the home-assistant/brands CDN, which has no KSM entry, and the
integration shows a placeholder everywhere."""
from pathlib import Path

from PIL import Image

BRAND = (Path(__file__).resolve().parents[2] / "custom_components"
         / "kiosk_satellite_manager" / "brand")


def test_brand_icons_ship_at_ha_sizes():
    """[KSM-TEST-325] brand/icon.png is 256x256 and icon@2x.png 512x512 RGBA
    PNGs with transparent corners and an opaque centre."""
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        with Image.open(BRAND / name) as image:
            assert image.format == "PNG"
            assert image.size == (size, size)
            rgba = image.convert("RGBA")
            last = size - 1
            for corner in ((0, 0), (last, 0), (0, last), (last, last)):
                assert rgba.getpixel(corner)[3] == 0, (name, corner)
            assert rgba.getpixel((size // 2, size // 2))[3] == 255


def test_brand_dir_holds_only_files_ha_serves():
    """[KSM-TEST-325] every file in brand/ is one HA's brands view allows."""
    allowed = {"icon.png", "icon@2x.png", "logo.png", "logo@2x.png", "dark_icon.png",
               "dark_logo.png", "dark_icon@2x.png", "dark_logo@2x.png"}
    shipped = {path.name for path in BRAND.iterdir()}
    assert "icon.png" in shipped
    assert shipped <= allowed
