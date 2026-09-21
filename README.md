# Kiosk Satellite Manager

A Home Assistant custom integration that provisions [Kiosk Satellite](https://github.com/jxlarrea/kiosk-satellite)
devices over network ADB — installing the app, granting its permissions, and applying its settings
in one pass, without a cable or a factory-reset window.

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
- **Credential safety** — KSM will not send Kiosk Satellite's admin password, a device bearer token,
  or a Home Assistant credential to the app's HTTP-only management service. Until Kiosk Satellite
  offers verified HTTPS, automatic device-name and Home Assistant credential sync is intentionally
  unavailable; installation and unauthenticated health checks still work.
- **Capability report** — the `kiosk_satellite_manager.capability_report` response service gathers a
  versioned, read-only evidence bundle for an unfamiliar Android device. It returns parsed platform,
  management, and Kiosk Satellite facts plus explicit probe status; it never returns raw shell output,
  account identifiers, credentials, or ADB keys.

Voice Satellite binding was explored and dropped (not worth the hassle yet) — see the design doc's
Non-goals if you're wondering why it isn't here.

Full design, verified findings, and phase-by-phase status: the [ham-harness](https://git.cfoxga.com/cfoxga/ham-harness)
repo's `kiosk-satellite-manager/` silo (`docs/SPEC/provisioning.md`).

## Installation

This repo lives on Gitea (`git.cfoxga.com`), not GitHub, so it is **not HACS-installable** — HACS
only adds repositories hosted on GitHub. Install manually: copy
`custom_components/kiosk_satellite_manager/` into your Home Assistant config's
`custom_components/` directory, then restart Home Assistant.

Then add the integration (Settings → Devices & Services → Add Integration → Kiosk Satellite Manager)
per device, and complete the three manual steps above on each device before the config flow can
connect.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).
