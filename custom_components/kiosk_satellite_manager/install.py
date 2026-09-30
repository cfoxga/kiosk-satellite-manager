"""Shared install/launch/permission-grant/onboarding sequence for a Kiosk
Satellite device -- used by both the Install/Reinstall button and
(KSM-BEHAVE-012) the config flow's auto-install step, so the two never
drift apart.

KSM-BEHAVE-007: `pm install` alone leaves the app installed but not running.
Confirmed live against the Test Portal: after `pm install -r -g` succeeded,
`/api/health` on :2324 still refused connections, because nothing had ever
launched the activity -- `am start` is required before the sensor can ever
read anything but "unavailable".

KSM-BEHAVE-008: `pm install -r -g` only auto-grants manifest-declared
runtime permissions. Battery-optimization exemption and "display over other
apps" have no runtime-permission equivalent and can't be requested through
`pm install` at all. The two commands below were pulled verbatim from Kiosk
Satellite's own web wizard source (`wizard.js`, fetched live from the Test
Portal's :2324 web UI), which hardcodes them as the fallback for devices
with no on-device settings screen for either toggle -- not guessed.

KSM-BEHAVE-010/011: once the app is up, sync its admin password and Device
Name to what was chosen at config-flow time, then connect it to this HA
instance with a freshly minted long-lived access token -- following the
device's own on-device setup wizard's own call shapes (`api/setup/password`
sets password + device name together on first run; `PATCH /api/settings` +
a `haCheckConnection` command handle the HA link either way, live-extracted
from wizard.js/settings.js). Best-effort: an entry created before this field
existed has no password (skipped, logged at debug), and a sync failure
(e.g. the web UI hasn't finished booting yet after `am start`) is logged as
a warning rather than failing the whole install/reinstall -- the app is
already installed and running by that point, which is the primary thing the
button promises.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import uuid
from dataclasses import dataclass
from datetime import timedelta

import aiohttp
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import get_url

from collections.abc import Callable, Mapping
from typing import Any, Awaitable, Final

from . import apk_cache, ks_api_client, ks_tls
from .adb_client import AdbClient
from .const import (
    CONF_REPLACE_LAUNCHER,
    DOMAIN,
    HA_TOKEN_LIFESPAN_DAYS,
    KS_APK_REMOTE_PATH,
    KS_HOME_ACTIVITY,
    KS_MAIN_ACTIVITY,
    KS_PACKAGE,
    SYNC_STATUS_POLL_ATTEMPTS,
    SYNC_STATUS_POLL_DELAY_S,
)
from .device_catalog import require_recipe
from .credentials import TokenCredential, async_revoke_owned_credential
from .install_recipes import InstallRecipe
from .helpers import target_release
from .ks_api import ApkAssetNotFound, latest_release
from .ks_api_client import KsApiError

_LOGGER = logging.getLogger(__name__)


class KsInstallVerificationFailed(Exception):
    """Phase 2 ("install and update"): a mutation's authoritative
    postcondition -- installed versionName, or /api/health's appVersion --
    never matched what was expected. Raised instead of trusting a zero ADB
    shell exit or a "Success" pm-install string alone once a version target
    is known (KSM-BEHAVE-040)."""


# KSM-BEHAVE-048 (issue #20): the module-level PORTAL_PERMISSIONS/PORTAL_APPOPS
# fallback lists are gone. They were what an *unmatched* device used to get --
# the full Meta Portal permission set, plus Device Admin, applied to hardware
# nobody had identified. There is no default permission set any more: a device
# with no approved recipe raises NoApprovedRecipe before any grant runs.

_BIND_ACCESSIBILITY_SERVICE: Final = "android.permission.BIND_ACCESSIBILITY_SERVICE"
_BIND_NOTIFICATION_LISTENER_SERVICE: Final = "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE"


@dataclass
class PermissionConvergenceResult:
    """KSM-BEHAVE-041 (Phase 3, "permission convergence"): what actually
    converged, read back from the device's own authoritative surface --
    never assumed from `pm grant`/`appops set`/`dumpsys deviceidle
    whitelist`'s shell exit. `accessibility`/`notification_listener` are
    each one of "granted", "not_applicable" (the installed KS build
    declares no such service), or "needs_user_interaction" (declared but
    still not showing enabled after the ADB write -- some OEM builds gate
    this behind an on-device tap regardless)."""

    granted_permissions: list[str]
    denied_permissions: list[str]
    granted_appops: list[str]
    denied_appops: list[str]
    battery_exempt: bool
    accessibility: str
    notification_listener: str

    @property
    def fully_converged(self) -> bool:
        return (
            not self.denied_permissions
            and not self.denied_appops
            and self.battery_exempt
            and self.accessibility != "needs_user_interaction"
            and self.notification_listener != "needs_user_interaction"
        )


async def _converge_consent_service(
    client: AdbClient,
    settings_key: str,
    enabled_flag_key: str | None,
    bind_permission: str,
    declared: dict[str, str],
) -> str:
    """KSM-BEHAVE-041: accessibility-service/notification-listener access
    are consent-adjacent surfaces (docs/developer/android-support/
    app-lifecycle.md) -- the settings string is additive-only (read the
    existing value first and append, never overwrite another app's already-
    enabled service) and read back afterward rather than assumed, since some
    OEM builds still gate this behind an on-device tap despite the ADB
    write."""
    component = declared.get(bind_permission)
    if component is None:
        return "not_applicable"

    current = await client.get_secure_setting(settings_key)
    parts = [p for p in current.split(":") if p]
    if component not in parts:
        parts.append(component)
        await client.put_secure_setting(settings_key, ":".join(parts))
    if enabled_flag_key:
        await client.put_secure_setting(enabled_flag_key, "1")

    after = await client.get_secure_setting(settings_key)
    after_parts = {p for p in after.split(":") if p}
    if component not in after_parts or not all(p in after_parts for p in parts):
        return "needs_user_interaction"
    if enabled_flag_key:
        flag = await client.get_secure_setting(enabled_flag_key)
        if flag != "1":
            return "needs_user_interaction"
    return "granted"


async def _converge_notification_listener(
    client: AdbClient, declared: dict[str, str]
) -> str:
    """Use NotificationManager's grant path and verify the active binding."""
    component = declared.get(_BIND_NOTIFICATION_LISTENER_SERVICE)
    if component is None:
        return "not_applicable"
    if await client.is_notification_listener_bound(component):
        return "granted"
    await client.shell(f"cmd notification allow_listener {shlex.quote(component)}")
    for attempt in range(3):
        if await client.is_notification_listener_bound(component):
            return "granted"
        if attempt < 2:
            await asyncio.sleep(0.25)
    return "needs_user_interaction"


async def grant_recipe_permissions(client: AdbClient, sdk: int, recipe: InstallRecipe) -> None:
    """The mutating half of convergence: request every grant. Callers must
    read the result back; a shell exit is never evidence."""
    for perm in recipe.permissions_for_sdk(sdk):
        await client.shell(f"pm grant {KS_PACKAGE} {perm}")
    for op in recipe.appops_for_sdk(sdk):
        await client.shell(f"appops set {KS_PACKAGE} {op} allow")
    if recipe.battery_exemption:
        await client.shell(f"dumpsys deviceidle whitelist +{KS_PACKAGE}")


async def converge_permissions(
    client: AdbClient, sdk: int, recipe: InstallRecipe
) -> PermissionConvergenceResult:
    """KSM-BEHAVE-041 (Phase 3, "permission convergence"): grant runtime
    permissions/AppOps/battery exemption, then read every one of them back
    from the device's own authoritative surface instead of trusting a shell
    exit. Converges accessibility-service and notification-listener access
    only when the installed KS build actually declares such a service
    (read from the device, never guessed), recording rather than claiming
    success when an on-device tap is still required."""
    await grant_recipe_permissions(client, sdk, recipe)
    perms = recipe.permissions_for_sdk(sdk)
    granted_now = await client.granted_permissions()
    granted_permissions = [p for p in perms if p in granted_now]
    denied_permissions = [p for p in perms if p not in granted_now]

    appops = recipe.appops_for_sdk(sdk)
    granted_appops = []
    denied_appops = []
    for op in appops:
        mode = await client.appop_mode(op)
        (granted_appops if mode == "allow" else denied_appops).append(op)

    battery_exempt = await client.is_battery_exempt()

    declared = await client.declared_bound_services()
    accessibility = await _converge_consent_service(
        client,
        "enabled_accessibility_services",
        "accessibility_enabled",
        _BIND_ACCESSIBILITY_SERVICE,
        declared,
    )
    notification_listener = await _converge_notification_listener(client, declared)

    result = PermissionConvergenceResult(
        granted_permissions=granted_permissions,
        denied_permissions=denied_permissions,
        granted_appops=granted_appops,
        denied_appops=denied_appops,
        battery_exempt=battery_exempt,
        accessibility=accessibility,
        notification_listener=notification_listener,
    )
    if not result.fully_converged:
        _LOGGER.warning("permission convergence incomplete for %s: %s", KS_PACKAGE, result)
    return result


_MIC_PERMISSION: Final = "android.permission.RECORD_AUDIO"
_CAMERA_PERMISSION: Final = "android.permission.CAMERA"
_BLUETOOTH_PERMISSIONS: Final = {
    "android.permission.BLUETOOTH_SCAN",
    "android.permission.BLUETOOTH_CONNECT",
}


@dataclass
class FunctionalVerificationResult:
    """KSM-BEHAVE-046 (Phase 5, "functional verification"): whether KS is
    actually positioned to behave correctly for microphone/camera/Bluetooth.
    Not full runtime-usage proof -- KSM can't safely trigger real audio
    capture or a live BT pairing on a shared lab device without side
    effects -- but the authoritative preconditions for that behavior: the
    runtime permission is actually granted (read back by
    `converge_permissions`, never assumed), and for Bluetooth, the device's
    own radio is on (`AdbClient.bluetooth_enabled`) -- a granted permission
    with a disabled radio is not "appropriate behavior" for a kiosk that
    depends on paired peripherals. Each field is one of "ok",
    "permission_denied", or (bluetooth only) "adapter_disabled"."""

    microphone: str
    camera: str
    bluetooth: str

    @property
    def fully_verified(self) -> bool:
        return self.microphone == "ok" and self.camera == "ok" and self.bluetooth == "ok"


async def verify_functional_capabilities(
    client: AdbClient, convergence: PermissionConvergenceResult
) -> FunctionalVerificationResult:
    """KSM-BEHAVE-046 (Phase 5): reuses Phase 3's already-authoritative
    permission readback rather than re-querying the device, plus one new
    device-level read (`bluetooth_enabled`) for the one Bluetooth signal
    that isn't a permission at all on SDK < 31 (the Test Portal is SDK 29,
    where `permissions_for_sdk` requests no BLUETOOTH_SCAN/CONNECT whatsoever
    -- confirmed live). Negative controls: a denied microphone/camera
    permission, or bluetooth permissions requested-but-denied, or the radio
    reading off, are each surfaced as a distinct non-"ok" status rather than
    silently reported as working."""
    granted = set(convergence.granted_permissions)
    requested = granted | set(convergence.denied_permissions)
    microphone = "ok" if _MIC_PERMISSION in granted else "permission_denied"
    camera = "ok" if _CAMERA_PERMISSION in granted else "permission_denied"

    required_bt = _BLUETOOTH_PERMISSIONS & requested
    if required_bt and not required_bt.issubset(granted):
        bluetooth = "permission_denied"
    elif not await client.bluetooth_enabled():
        bluetooth = "adapter_disabled"
    else:
        bluetooth = "ok"

    result = FunctionalVerificationResult(microphone=microphone, camera=camera, bluetooth=bluetooth)
    if not result.fully_verified:
        _LOGGER.warning("functional verification incomplete for %s: %s", KS_PACKAGE, result)
    return result


def launcher_replacement_wanted(entry_data: Mapping[str, Any], recipe: InstallRecipe) -> bool:
    """KSM-BEHAVE-143: an explicit per-device choice wins; unset follows the recipe."""
    explicit = entry_data.get(CONF_REPLACE_LAUNCHER)
    if explicit is not None:
        return bool(explicit)
    return recipe.replaces_launcher_by_default


async def _select_home_launcher(
    hass: HomeAssistant, client: AdbClient, host: str | None
) -> None:
    """Select KS's HOME alias and trust only Android's resolver readback.

    A miss is reported, never fatal: the device-name and HA sync must still run.
    """
    last_resolver = ""
    for attempt in range(SYNC_STATUS_POLL_ATTEMPTS):
        await client.select_ks_home()
        last_resolver = await client.resolved_home_activity()
        if KS_HOME_ACTIVITY in {line.strip() for line in last_resolver.splitlines()}:
            return
        if attempt < SYNC_STATUS_POLL_ATTEMPTS - 1:
            await asyncio.sleep(SYNC_STATUS_POLL_DELAY_S)
    _LOGGER.warning("home launcher not selected on %s: resolver=%r", host, last_resolver)
    persistent_notification.async_create(
        hass,
        message=(
            f"Kiosk Satellite was installed on {host}, but Android kept another app as "
            f"the Home screen. Resolver: {last_resolver!r}"
        ),
        title="Kiosk Satellite is not the Home screen",
        notification_id=f"{DOMAIN}_home_launcher_{host}",
    )


async def install_and_launch(
    hass: HomeAssistant,
    client: AdbClient,
    session: aiohttp.ClientSession,
    host: str | None = None,
    device_name: str | None = None,
    password: str | None = None,
    ha_token: str | None = None,
    token_credential: TokenCredential | None = None,
    device_model: str | None = None,
    ha_url: str | None = None,
    on_tls_pinned: Callable[[str], None] | None = None,
    before_ha_setup: Callable[[], Awaitable[None]] | None = None,
    replace_launcher: bool = False,
) -> TokenCredential | None:
    """Fetch the latest universal KS APK (#71), install it, launch
    it, and grant full permissions. If a password is configured on the entry,
    also sync the device's admin password/Device Name and connect it to this
    HA instance (KSM-BEHAVE-010/011/014/015/020). Returns the HA token used.

    `device_model` is an exact `device_models` key. Resolution happens first,
    before any device mutation: a model with no approved recipe assignment
    raises `NoApprovedRecipe` here rather than being provisioned on a guess
    (KSM-BEHAVE-048).

    `on_tls_pinned` receives the device's HTTPS key pin once the sync has
    established it (KSM-BEHAVE-094); the caller persists it."""
    recipe = require_recipe(device_model)
    try:
        sdk_str = await client.getprop("ro.build.version.sdk")
        sdk = int(sdk_str) if sdk_str.isdigit() else 29
    except Exception:
        sdk = 29

    # #74: the device's ABI list picks its split (KSM-BEHAVE-107).
    abis: list[str] = []
    for prop in ("ro.product.cpu.abilist", "ro.product.cpu.abi"):
        try:
            abis = [a.strip() for a in (await client.getprop(prop)).split(",") if a.strip()]
        except Exception:  # noqa: BLE001 -- unknown ABIs fall back to universal
            abis = []
        if abis:
            break

    # KSM-BEHAVE-114/147: the pinned version, else the last successful shared
    # release check's latest (a later rate-limited check keeps it, #107). Only
    # an install before any check succeeded asks GitHub here.
    release, apk_url = target_release(hass), None
    if release is not None:
        target_version = release.version
    else:
        try:
            apk_url, target_version = await latest_release(session, abis)
        except (aiohttp.ClientError, asyncio.TimeoutError, ApkAssetNotFound) as err:
            raise HomeAssistantError(
                f"Kiosk Satellite release lookup failed ({err}); pin a downloaded "
                "version in Kiosk Satellite Manager's Install version to install it"
            ) from err
    current_version = await client.installed_version()
    if target_version and current_version == target_version:
        # KSM-BEHAVE-040 (Phase 2, "preserve compatible installations where
        # possible"): the device is already running the release we'd fetch,
        # so skip download/push/install entirely rather than reinstalling
        # over a working app. `am start`/permission grants below still run
        # every press -- they are themselves mutations with their own
        # postconditions, per the acceptance text.
        _LOGGER.debug(
            "kiosk satellite on %s already at %s; preserving install", host, target_version
        )
    else:
        # KSM-BEHAVE-107: the same verified cache the API update path uploads
        # from. KSM-BEHAVE-063: async_cached_apk checks the signer pin before
        # the file is stored, so nothing unverified is ever pushed.
        universal = None
        if release is None and target_version:
            # #74: a universal APK already cached (#71) is reused, as on the
            # API path, rather than downloading the split.
            universal = await hass.async_add_executor_job(
                apk_cache.cached_device_apk, apk_cache.cache_root(hass), target_version, ()
            )
        if release is not None:
            apk = await apk_cache.async_release_apk(hass, release, abis)
        elif universal is not None:
            apk = universal
        else:
            apk = await apk_cache.async_cached_apk(
                hass,
                session,
                target_version or "unversioned",
                apk_url.rsplit("/", 1)[-1],
                apk_url,
            )
        await client.push(str(apk), KS_APK_REMOTE_PATH)
        try:
            # KSM-BEHAVE-035/063: rejected artifacts, including signer
            # mismatch, abort here. Never uninstall and retry as a fresh
            # install; that would bypass certificate continuity.
            await client.install_apk(KS_APK_REMOTE_PATH)
        finally:
            await client.shell(f"rm -f {KS_APK_REMOTE_PATH}")

        installed_version = await client.installed_version()
        if target_version and installed_version != target_version:
            raise KsInstallVerificationFailed(
                f"installed versionName {installed_version!r} does not match "
                f"target {target_version!r} on {host} after install"
            )

    start_output = await client.shell(f"am start -n {KS_MAIN_ACTIVITY}")
    if "Error" in start_output:
        raise KsInstallVerificationFailed(
            f"am start -n {KS_MAIN_ACTIVITY} reported an error on {host}: {start_output.strip()}"
        )
    convergence = await converge_permissions(client, sdk, recipe)
    await verify_functional_capabilities(client, convergence)
    if recipe.sets_device_admin:
        await client.shell(f"dpm set-active-admin {KS_PACKAGE}/.KioskAdminReceiver")
    if host is not None:
        # KSM-BEHAVE-040 (Phase 2): "am start exit 0 proves nothing" applies
        # to the preserve/skip branch too -- am start is itself a mutation
        # every press, so its postcondition (the app actually came up and
        # reports the expected version) is read back unconditionally here,
        # not only right after a fresh install.
        await _verify_health(session, host, target_version)

    if password is None or host is None:
        _LOGGER.debug(
            "no admin password configured for %s; skipping device-name/HA auto-connect sync",
            host,
        )
        if replace_launcher:
            await _select_home_launcher(hass, client, host)
        return None
    credential = None
    try:
        credential = await _sync_device_and_connect_ha(
            hass,
            session,
            host,
            device_name or host,
            password,
            ha_token=ha_token,
            token_credential=token_credential,
            recipe=recipe,
            ha_url=ha_url,
            on_tls_pinned=on_tls_pinned,
            before_ha_setup=before_ha_setup,
            replace_launcher=replace_launcher,
        )
    except (KsApiError, aiohttp.ClientError, asyncio.TimeoutError) as err:
        _LOGGER.warning("device-name/HA auto-connect sync failed for %s: %s", host, err)
    if replace_launcher:
        await _select_home_launcher(hass, client, host)
    return credential



async def _verify_health(
    session: aiohttp.ClientSession, host: str, target_version: str | None
) -> None:
    """KSM-BEHAVE-040 (Phase 5 precursor -- "KS health"): bounded poll of the
    device's own /api/health, the same authoritative readback channel
    provisioning.py uses, instead of trusting `am start`'s shell exit. If
    target_version is unknown (e.g. the releases API didn't return a
    tag_name), only reachability is checked."""
    last: str = "never reachable"
    for attempt in range(SYNC_STATUS_POLL_ATTEMPTS):
        try:
            health = await _read_health_any(session, host)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            last = str(err)
        else:
            if not target_version or health.get("appVersion") == target_version:
                return
            last = f"appVersion={health.get('appVersion')!r}"
        if attempt < SYNC_STATUS_POLL_ATTEMPTS - 1:
            await asyncio.sleep(SYNC_STATUS_POLL_DELAY_S)
    raise KsInstallVerificationFailed(
        f"/api/health on {host} never confirmed appVersion={target_version!r}: {last}"
    )


async def _read_health_any(session: aiohttp.ClientSession, host: str) -> dict:
    """Unauthenticated health on whichever transport the device serves right
    now -- a reinstall keeps `remote.tls`, a factory reset clears it."""
    probed = await ks_api_client.probe_https(session, host)
    if probed is not None:
        return probed[1]
    return await ks_api_client.get_health(session, host, pin=None)


async def _wait_for_setup_status(
    session: aiohttp.ClientSession, host: str
) -> tuple[dict, str | None]:
    """(setup status, served HTTPS key or None when the device is on HTTP)."""
    last_err: Exception | None = None
    for attempt in range(SYNC_STATUS_POLL_ATTEMPTS):
        probed = await ks_api_client.probe_https(session, host, "/api/setup/status")
        if probed is not None:
            return probed[1], probed[0]
        try:
            return await ks_api_client.get_setup_status(session, host, pin=None), None
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            last_err = err
            if attempt < SYNC_STATUS_POLL_ATTEMPTS - 1:
                await asyncio.sleep(SYNC_STATUS_POLL_DELAY_S)
    raise KsApiError(f"setup status never became reachable: {last_err}")


async def _mint_ha_token(hass: HomeAssistant, client_name: str) -> TokenCredential:
    """Create a dedicated, local-only read-only credential for one kiosk.

    Kiosk Satellite only needs to render Home Assistant; it must never inherit
    an operator's owner authority. The built-in read-only group denies entity
    control and all administrator-only APIs. A local-only user also prevents a
    compromised kiosk token from being used through HA's public endpoint.
    """
    device_name = client_name.removeprefix("Kiosk Satellite Manager - ").split(" [", 1)[0]
    user = await hass.auth.async_create_user(
        f"Kiosk Satellite - {device_name}",
        group_ids=[GROUP_ID_READ_ONLY],
        local_only=True,
    )
    refresh_token = await hass.auth.async_create_refresh_token(
        user,
        client_name=client_name,
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=HA_TOKEN_LIFESPAN_DAYS),
    )
    return TokenCredential(
        access_token=hass.auth.async_create_access_token(refresh_token),
        refresh_token_id=refresh_token.id,
        owned=True,
    )


async def _sync_device_and_connect_ha(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    host: str,
    device_name: str,
    password: str,
    *,
    recipe: InstallRecipe,
    ha_token: str | None = None,
    token_credential: TokenCredential | None = None,
    ha_url: str | None = None,
    on_tls_pinned: Callable[[str], None] | None = None,
    before_ha_setup: Callable[[], Awaitable[None]] | None = None,
    replace_launcher: bool = False,
) -> TokenCredential:
    status, served = await _wait_for_setup_status(session, host)
    password_needed = status.get("passwordNeeded", True)
    token: str | None = None
    if password_needed:
        # The one accepted plaintext bootstrap on an HTTP device: first-run
        # password + Device Name (KSM-BEHAVE-094).
        token = await ks_api_client.setup_password(
            session, host, password, device_name, pin=served
        )
    # KSM-BEHAVE-094: an operator trust event -- re-establish the pin rather
    # than reuse a stored one. Raises (before any HA credential is sent) if a
    # TLS-capable device cannot be switched; None means the KS predates TLS.
    pin = await ks_tls.async_establish_tls(session, host, password)
    if served is not None and pin != served:
        raise KsApiError(f"{host} changed its TLS key during onboarding; nothing pinned")
    if pin is not None and on_tls_pinned is not None:
        on_tls_pinned(pin)
    if pin is not None or token is None:
        # The token that authorizes the HA-token PATCH never crossed HTTP.
        token = await ks_api_client.login(session, host, password, pin=pin)
    if not password_needed and status.get("deviceName") != device_name:
        await ks_api_client.patch_settings(
            session, host, token, {"device.name": device_name}, pin=pin
        )

    # Fleet sync needs a running app and a local admin credential. Invite at
    # that first safe point, before KSM writes Home Assistant settings.
    if before_ha_setup is not None:
        await before_ha_setup()

    credential = token_credential
    created_credential = False
    if credential is None and ha_token:
        credential = TokenCredential(ha_token, None, owned=False)
    if credential is None:
        # HA requires client names to be unique across refresh tokens. A
        # reset/reprovisioned kiosk may have left a revoked-but-stored token
        # with the same user-visible name, so retain that name for operators
        # and add a short opaque suffix for the token identity.
        client_name = f"Kiosk Satellite Manager - {device_name} [{uuid.uuid4().hex}]"
        credential = await _mint_ha_token(hass, client_name)
        created_credential = True
    try:
        ha_url = (ha_url or get_url(hass, prefer_external=False)).rstrip("/")
        # KSM-BEHAVE-049: the start path is recipe data, with no "/portal if we
        # can't tell" default -- an unidentified device never reaches this call.
        start_path = recipe.start_url_path
        start_url = f"{ha_url}{start_path}" if start_path else ha_url
        settings_payload: dict[str, Any] = {
            "ha.url": ha_url,
            "ha.token": credential.access_token,
            "browser.start_url": start_url,
            # KSM-BEHAVE-061: clear KSM's legacy unsafe browser setting.
            "browser.ignore_ssl_errors": False,
        }
        if replace_launcher:
            settings_payload["home.enabled"] = True

        await ks_api_client.patch_settings(session, host, token, settings_payload, pin=pin)
        connected, error = await ks_api_client.check_ha_connection(session, host, token, pin=pin)
        if not connected:
            detail = str(error or "no error detail supplied")
            for secret in (password, token, credential.access_token):
                if secret:
                    detail = detail.replace(secret, "[redacted]")
            _LOGGER.warning(
                "Kiosk Satellite reported the HA connection check failed for %s: %s",
                host, detail,
            )
    except Exception:
        if created_credential:
            await async_revoke_owned_credential(hass, credential)
        raise
    return credential
