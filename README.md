# Kiosk Satellite Manager

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![GitHub Release](https://img.shields.io/github/v/release/cfoxga/kiosk-satellite-manager)](https://github.com/cfoxga/kiosk-satellite-manager/releases)
[![Validate](https://github.com/cfoxga/kiosk-satellite-manager/actions/workflows/validate.yml/badge.svg)](https://github.com/cfoxga/kiosk-satellite-manager/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/cfoxga/kiosk-satellite-manager)](LICENSE)

A Home Assistant integration that installs, onboards and manages
[Kiosk Satellite](https://github.com/jxlarrea/kiosk-satellite) on Meta Portals and Android TV
devices: network ADB for the install, then Kiosk Satellite's authenticated management API for
updates, fleets, backups and settings.

> Not affiliated with the Kiosk Satellite project.

## Installation

Requires Home Assistant 2025.3 or later.

### HACS (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cfoxga&repository=kiosk-satellite-manager&category=integration)

Click the button, then **Download**, and restart Home Assistant. Or add it by hand: **HACS → ⋮ →
Custom repositories**, add `https://github.com/cfoxga/kiosk-satellite-manager` with type **Integration**, then find **Kiosk Satellite Manager**
and download it. HACS installs the latest [release](https://github.com/cfoxga/kiosk-satellite-manager/releases) and offers each new one as an
update.

### Manual

Download the latest [release](https://github.com/cfoxga/kiosk-satellite-manager/releases), copy `custom_components/kiosk_satellite_manager/`
into your Home Assistant config's `custom_components/` directory, and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up Kiosk Satellite Manager.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=kiosk_satellite_manager)

Or go to **Settings → Devices & services → Add integration → Kiosk Satellite Manager**.

1. Choose **Configure KSM** once. This creates **KSM Settings**, which holds the defaults for new
   devices and the release controls.
2. Add each kiosk through the same **Add integration** path, entering its IP address.

### Before you add a device

Everything else is automated, but these happen on the physical device and cannot be driven
remotely. Do them once per device, in order:

1. **Enable ADB** on the device (on a Meta Portal this is gated behind onboarding plus an
   account/PIN-gated toggle; on an Android TV device it's Settings → About → tap Build 7 times →
   Developer options → enable ADB debugging).
2. **Tap "Allow USB debugging?"** on the device screen the first time this integration connects.
   The integration reuses one persistent ADB key across devices, so this is a true one-time tap per
   device, not a per-session prompt.
3. **Tap Accept on-device if the device is joining a Kiosk Satellite fleet.** Choose an existing
   Fleet in KSM's Add device form to have its leader send the invitation once KS is running.
   Invitations are answered on the new kiosk screen only; KSM moves the device from Unmanaged
   after KS confirms acceptance.

## Features

- **Native fleets** — Home Assistant shows a global KSM entry, one Unmanaged entry, and one entry
  per Kiosk Satellite fleet leader. Managed kiosks appear as device subentries under their confirmed
  leader, or under Unmanaged until they join a managed fleet. KSM reads authenticated `fleetStatus`
  during health checks; failed reads keep the last confirmed placement. Fleet entries show the
  leader, managed count, reachability and sync status. Existing device entries migrate in place:
  their entity IDs, areas, options, service target IDs and backup paths remain usable. Configure a
  physical kiosk from its device subentry to update its password or enable Device Owner.
  A new device can select an existing Fleet during addition. For a fresh install KSM sends the
  invitation after setting the local admin password and secure transport, before writing its HA
  settings. KS still keeps identity, remote access, HA credentials, and hardware preferences local;
  the selected Fleet syncs only the categories allowed by its Default profile after acceptance.
- **Install** — connects over ADB, detects the device type (Meta Portal vs. Android TV stick, etc.),
  fetches the matching Kiosk Satellite APK for the device's ABI from the project's GitHub releases,
  and installs it. Each device gets an **Install Kiosk Satellite** button (install or reinstall) and
  an **Uninstall Kiosk Satellite** button under Configuration. With the device's **Replace launcher**
  option on (the default for Portal recipes, off for Android TV / onn devices), Install also makes
  Kiosk Satellite the Android Home app and reads the resolver back; a miss raises a notification and
  the install still completes. A device that already runs Kiosk Satellite can be added without ADB:
  KSM reads it over its own API and asks only for its existing admin password.
- **ESPHome** — the Add form's **Enable ESPHome** checkbox (defaulting to the manager option) turns
  on the kiosk's ESPHome server and its ESPHome entities, waits for Home Assistant to discover it, and
  adds it as an ESPHome device with its encryption key. A failure raises a notification and the device
  is still added to KSM.
- **Update** — KSM creates no `update` entity or version sensor. Turn on the kiosk's ESPHome
  server and add it to Home Assistant as an ESPHome device; ESPHome's update entity is what shows
  Kiosk Satellite releases in Settings → Updates. KSM checks GitHub for releases every 15 minutes
  (or now, with the manager's **Check for updates** button). A per-device **Auto-update Kiosk
  Satellite** switch (off by default) installs a newer release through the same verified path as
  the Install button, trying each version at most once per load. The manager's **Install version**
  option pins a release, and **Hide follower updates** keeps fleet followers from each showing
  their own update prompt.
- **Global controls** — choose Configure KSM in the integration's Add flow to create one
  manager entry, **KSM Settings**. Its Configure form sets defaults for future devices: reuse
  or reinstall an existing app, auto-update, Enable ESPHome, Kiosk Satellite password, Home
  Assistant URL, and a dedicated device token or an existing token ID. It also holds the
  fleet-wide **Install version**, **Hide follower updates**, and backup period and retention.
  The manager stores no token value. Review mode lets you override defaults per device.
  Automatic mode still requires an ADB address, on-device authorization, and a confirmation of
  the selected actions. Changing manager settings does not rewrite existing device settings.
- **Device Configure menu** — each kiosk's Configure offers **Rename device**, **Update Kiosk
  Satellite password**, **Change host** (DNS name or IP address, verified against the device's
  saved key), **Replace launcher**, and **Enable Device Owner (advanced)**. Device Owner runs a
  read-only check, explains its side effects, and changes nothing until you confirm; on Meta
  Portals it then reopens Meta setup so you can sign back in on the device. On an Android 9 Portal
  Gen 1, the same entry opens a confirmed Meta cleanup instead. See
  [Device Owner on Meta Portals](docs/device-owner.md) for how this works without a factory reset.
- **Configuration backup** — **Back up configuration** saves Kiosk Satellite's own settings export
  to a dated file under `config/kiosk_satellite_manager/backups/`, automatically every 24 hours by
  default (10 kept per device). **Restore configuration** imports the file chosen in
  **Configuration backup** after a safety backup, keeping the kiosk's current password and Home
  Assistant token. KSM Settings' **Backup All** backs up every loaded device. The files contain the
  kiosk's passwords and token; treat them as secrets.
- **Permissions and diagnostics** — a **Permissions** problem sensor lists any permission, AppOp or
  battery exemption the installed Kiosk Satellite declares but does not have, and **Fix
  permissions** re-grants them over ADB. Each device also shows **ADB enabled**, **IP address**,
  **Device type**, **Install recipe** and **Fleet membership** diagnostics.
- **Discovered followers** — when KSM creates a fleet entry, followers on that leader's roster that
  KSM doesn't manage yet appear under Discovered; a follower that joins later raises one repair
  asking whether to add it. KSM never changes fleet membership itself.
- **Fleet release and updates** — the manager entry exposes the latest usable Kiosk
  Satellite release and check status, plus Update all. The button visits managed,
  reachable devices with an older version through the same verified install
  path as each device's Install button. A notification names updated, skipped, and failed
  devices. Current devices are skipped.
- **Provision** — `kiosk_satellite_manager.provision` logs in with the stored device password and
  sends one `PATCH /api/settings`. It accepts only `device.name`, `device.hostname`,
  `remote.enabled`, `remote.password`, and `esphome.node_name`, with the types shown in the
  service form. HA URL, dashboard and kiosk-lockdown keys are not accepted by this service.
  Per-key API rejection fails the call; `/api/health` additionally verifies `device.name`
  when supplied. Other accepted keys are not independently health-readback verified.
- **Credential transport** — management uses pinned HTTPS on `:2324` once KSM has enabled
  HTTPS and stored the device key (Kiosk Satellite 2026.9.78 or later). A changed key blocks
  requests until the repair is resolved. Older/unpinned devices use HTTP, so credentials on
  those connections remain observable on the management network. ADB remains the path for
  onboarding, Install/Reinstall and Uninstall; the provision service does not use ADB.
- **Capability report** — the `kiosk_satellite_manager.capability_report` response service gathers a
  versioned, read-only evidence bundle for an unfamiliar Android device. It returns parsed platform,
  management, and Kiosk Satellite facts plus explicit probe status; it never returns raw shell output,
  account identifiers, credentials, or ADB keys. The response-only
  `kiosk_satellite_manager.onboarding_plan` service turns that evidence into a deterministic,
  explained dry-run plan; it executes nothing.
- **Rename device** — the `kiosk_satellite_manager.rename_device` service renames a device's Kiosk
  Satellite identity (name, hostname, ESPHome node name) over the authenticated `:2324` settings
  API, then updates the KSM entry's title/name and migrates its DNS host once the new hostname is
  verified to resolve to the same device. Returns a per-layer result instead of raising on a
  device-side failure; Android's own system device name has no verified non-ADB write path yet and
  is reported as `unsupported`. It then reloads the device's ESPHome entry, matched by IP, so
  Home Assistant registers the ESPHome actions under the new name, removes the old-name actions
  it replaced (HA leaves them behind, dead, until a restart), and lists any automations or
  scripts that still call the old action names (reported only, never edited).

KSM does not manage Voice Satellite. Kiosk Satellite 2026.9.87 and later has it built in: turn on
the kiosk's ESPHome server and Voice Satellite on the kiosk, then add it in Home Assistant as an
ESPHome device. The separate Voice Satellite integration is no longer needed.

## Device and recipe reference

The KSM Settings entry has native diagnostic entities for the
[device catalog](docs/supported-devices.md) and
[install recipes and settings](docs/install-recipes.md). Open **KSM Settings**
from the integration page, then select a diagnostic entity and choose
**Menu → Details** to browse its attributes. Each managed
device also has an **Install recipe** diagnostic entity with that device's
approved recipe policy. Assignment and build-specific qualification are
separate; the [device catalog page](docs/supported-devices.md) explains the
boundary and known limitations.

## Release notes

See [CHANGELOG.md](CHANGELOG.md).

## Issues and contributing

Report bugs and requests on [GitHub Issues](https://github.com/cfoxga/kiosk-satellite-manager/issues). See [CONTRIBUTING.md](CONTRIBUTING.md) for
development setup.

When an install or Device Owner enrollment goes wrong, attach the entry's **Download
diagnostics** file (Settings → Devices & services → Kiosk Satellite Manager → ⋮). It lists what
KSM ran on the device and whether the device rebooted, and holds no passwords, tokens,
addresses or names.

## License

[MIT](LICENSE)
