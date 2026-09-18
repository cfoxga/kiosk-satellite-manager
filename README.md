# Kiosk Satellite Manager

A Home Assistant custom integration that provisions [Kiosk Satellite](https://github.com/jxlarrea/kiosk-satellite)
devices over network ADB — installing the app, granting its permissions, and applying its settings
in one pass, without a cable or a factory-reset window. It replaces DroidMesh's provisioning role;
DroidMesh's peer-mesh and resident-agent features are retired, not ported, since Kiosk Satellite now
manages its own fleet natively.

Not an officially affiliated Kiosk Satellite product — "Manager" in the name is deliberate, cleared
with the KS author (jxlarrea) before going live.

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
  are provided per device.
- **Provision** — the `kiosk_satellite_manager.provision` service applies a settings payload (device
  name, Home Assistant URL, dashboard, kiosk lockdown, etc.) in a single ADB intent and reads back
  `/api/health` to confirm the change actually took, rather than trusting `adb shell`'s exit code.
- **Voice Satellite binding** — experimental, currently blocked pending an on-device ADB-enable step
  on the test device; may land as a documented manual step instead of an automated one. See
  `docs/SPEC/provisioning.md` for status.

Full design, verified findings, and phase-by-phase status: the [ham-harness](https://git.cfoxga.com/cfoxga/ham-harness)
repo's `kiosk-satellite-manager/` silo (`docs/SPEC/provisioning.md`).

## Installation

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cfoxga&repository=kiosk-satellite-manager&category=integration)

Not yet in the default HACS store — the badge above adds this repo to HACS as a custom repository.
Manually: add `https://git.cfoxga.com/cfoxga/kiosk-satellite-manager` as an Integration-type custom
repository in HACS, install **Kiosk Satellite Manager**, and restart Home Assistant.

Then add the integration (Settings → Devices & Services → Add Integration → Kiosk Satellite Manager)
per device, and complete the three manual steps above on each device before the config flow can
connect.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).
