# Kiosk Satellite Manager

A Home Assistant custom integration that provisions [Kiosk Satellite](https://github.com/jxlarrea/kiosk-satellite)
devices using network ADB for installation and onboarding, then the authenticated Kiosk Satellite
management API for routine configuration.

## Three manual steps you can't script away

Everything else is automated, but these three happen on the physical device and cannot be driven
remotely — do them once per device, in order:

1. **Enable ADB** on the device (on a Meta Portal this is gated behind onboarding plus an
   account/PIN-gated toggle; on an Android TV device it's Settings → About → tap Build 7 times →
   Developer options → enable ADB debugging).
2. **Tap "Allow USB debugging?"** on the device screen the first time this integration connects.
   The integration reuses one persistent ADB key across devices, so this is a true one-time tap per
   device, not a per-session prompt.
3. **Tap Accept on-device if the device is joining a Kiosk Satellite fleet.** Fleet invitations are
   answered on the kiosk screen only — this integration does not attempt fleet attach at all (see
   Non-goals in the design doc).

## What it does

- **Install** — connects over ADB, detects the device type (Meta Portal vs. Android TV stick, etc.),
  fetches the matching Kiosk Satellite APK for the device's ABI from the project's GitHub releases,
  and installs it. A `button` entity (Install/Reinstall) and a `sensor` entity (installed KS version)
  are provided per device. On launcher-capable devices, the same ADB-only path enables Kiosk
  Satellite's fixed Home alias and verifies Android's HOME resolver selected it.
- **Update** — each device gets an `update` entity that compares the installed KS version with the
  latest GitHub release. The release check runs once an hour for all devices, or on demand with
  `homeassistant.update_entity`. Installing from it runs the same verified path as the Install
  button. Updates show up in Home Assistant's Settings → Updates without ESPHome. A per-device
  `Auto-update Kiosk Satellite` switch (off by default) installs new releases automatically. It
  never installs a version you skipped, and it tries each version only once.
- **Global controls** — choose Configure KSM in the integration's Add flow to create one
  manager entry. Its Configure form sets defaults for future devices: reuse or reinstall
  an existing app, home launcher, auto-update, Kiosk Satellite password, Home Assistant
  URL, and a dedicated device token or an existing token ID. The manager stores no token
  value. Review mode lets you override defaults per device. Automatic mode still requires
  an ADB address, on-device authorization, and a confirmation of the selected actions.
  Changing manager settings does not rewrite existing device entries.
- **Fleet release and updates** — the manager device exposes the latest usable Kiosk
  Satellite release and check status, plus Update all. The button visits managed,
  reachable devices with an unskipped newer version through the same verified install
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
  account identifiers, credentials, or ADB keys.
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

Full design, verified findings, and phase-by-phase status: the [ham-harness](https://git.cfoxga.com/cfoxga/ham-harness)
repo's `kiosk-satellite-manager/` silo (`docs/SPEC/provisioning.md`).

## Installation

This repo lives on Gitea (`git.cfoxga.com`), not GitHub, so it is **not HACS-installable** — HACS
only adds repositories hosted on GitHub. Install manually: copy
`custom_components/kiosk_satellite_manager/` into your Home Assistant config's
`custom_components/` directory, then restart Home Assistant.

Then add the integration (Settings → Devices & Services → Add Integration → Kiosk Satellite Manager).
Choose Configure KSM once for global defaults and release controls, then add each device through
the same Add Integration path. Complete the manual steps above on each device before its flow can
connect.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).
