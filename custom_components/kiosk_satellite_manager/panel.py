"""Manager-owned, admin-only sidebar panel registration."""

from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig

from .const import DOMAIN

PANEL_NAME = "kiosk-satellite-manager"
_STATIC_URL = f"/api/{DOMAIN}/panel"
_STATIC_REGISTERED_KEY = f"{DOMAIN}_panel_static_registered"


async def async_register_panel(hass):
    """Install the static route once per HA process and the sidebar on reload."""
    path = Path(__file__).parent / "www"
    version = await hass.async_add_executor_job(
        lambda: max((int(item.stat().st_mtime_ns) for item in path.rglob("*.js")), default=0)
    )
    if not hass.data.get(_STATIC_REGISTERED_KEY):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_STATIC_URL, str(path), False)]
        )
        hass.data[_STATIC_REGISTERED_KEY] = True
    frontend.async_register_built_in_panel(
        hass, component_name="custom", sidebar_title="Kiosk Satellite Manager",
        sidebar_icon="mdi:tablet-dashboard", frontend_url_path=PANEL_NAME,
        config={"_panel_custom": {
            "name": "ksm-panel",
            "module_url": f"{_STATIC_URL}/ksm-panel.js?v={version}",
        }}, require_admin=True,
    )


def async_remove_panel(hass):
    """Remove the sidebar while retaining the process-scoped static route."""
    frontend.async_remove_panel(hass, PANEL_NAME)
