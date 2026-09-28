# Changelog

## Unreleased

- KSM now manages each kiosk's Voice Satellite entry (#65). When a device is
  set up, KSM reuses the Voice Satellite entry with the device's name, or
  creates one, and points Kiosk Satellite at its satellite entity. A kiosk
  that is already bound to a working Voice Satellite entity keeps it, unless
  that satellite carries another kiosk's name (a renamed kiosk gets its own). If Voice
  Satellite isn't installed, a repair asks whether to install it through HACS;
  nothing is downloaded unless you confirm.

- Adding a Portal no longer stops half-way when Android keeps its own Home screen
  (#61). Install/Reinstall used to fail before it set the device name, admin
  password and Home Assistant connection whenever the Portal's launcher stayed
  Home. Now it finishes the setup and raises a notification that Kiosk Satellite
  isn't the Home screen. Portal (2nd gen) no longer tries to take over Home at
  all, the same as Portal Go.

- Add Device can enable Device Owner (#62). Tick **Enable Device Owner
  (advanced)** when adding a device. After install, KSM runs the same read-only
  check and confirmation as the Configure menu. The device is added whatever
  happens, and a notification reports whether Device Owner was enabled, skipped
  or blocked, and why. Default is off.

- Add a device that already runs Kiosk Satellite without enabling ADB (#60). When
  the address answers Kiosk Satellite's `/api/health`, Add Device reads the model,
  Android version and name from it and asks only for the device's existing admin
  password. The password is checked against the device, which is switched to
  pinned HTTPS when its version supports it. Nothing is installed. ADB is still
  needed for Install/Reinstall and the other ADB-only actions. Addresses that don't
  answer as Kiosk Satellite keep the existing ADB setup.

- Talk to Kiosk Satellite devices over HTTPS instead of plain HTTP (#57). On a
  device running Kiosk Satellite 2026.9.78 or later, KSM turns on the device's
  HTTPS setting and remembers the device's key. After that, the admin password
  and login token only go to a device that presents that same key, and a changed
  key blocks management and raises a repair you confirm to trust the new one.
  Existing devices switch over automatically the first time Home Assistant loads
  KSM after this update. Older Kiosk Satellite versions stay on HTTP and log a
  warning naming the version needed.

- Fix `rename_device` leaving a kiosk's ESPHome actions under the old name. After a
  verified rename, KSM finds the one ESPHome entry at the same IP, waits for Home
  Assistant to record the new node name, and reloads that entry so
  `esphome.<new_node>_*` actions register. The result gains `esphome`
  (`applied`/`unchanged`/`pending`/`not_found`/`failed`) and, when the node name
  changed, `esphome_actions` listing the automations and scripts that still call
  the old action names. KSM reports those callers and edits none of them. The
  ESPHome node name now matches the hostname (`great-room-portal`), since Kiosk
  Satellite uses hyphens anyway.

- Fix two `rename_device` gaps found testing against a real kiosk. Renaming to
  the name the device already shows (e.g. after a fresh install left the node as
  `ks-<name>`) skipped the update and then waited 60 s to report `pending`; KSM
  now reads the device's settings and only skips when the name, hostname and
  node name all already match. And Home Assistant never removes ESPHome actions
  on reload, so each rename left a set of dead `esphome.<old_node>_*` actions
  behind until the next restart; KSM now removes the old ones once the same
  actions exist under the new name, and lists them in `esphome_actions.removed`.

- Add **Enable Device Owner** to a device's Configure menu. It explains what
  Device Owner gives Kiosk Satellite (silent self-updates, true kiosk lock, home
  app without a prompt, remote reboot, real Wi-Fi MAC) and its side effects (only
  a factory reset undoes it; the device's accounts are signed out), lists the
  account types found, and changes nothing until you tick the confirmation. On
  models with verified support (Portal Mini today) the blocking account app is
  removed for a moment and then reinstalled, so no factory reset is needed.
  Device Configure now opens a menu; the password form is one of its entries.
  Dialog text now ships in `translations/en.json`, so KSM dialogs show their
  descriptions.

- Fix self-update status parsing for Kiosk Satellite's `{ok, data}` command
  response. An offered release can now reach the install step; failed or
  malformed status responses stop before installation.

- Add a per-device Configure form to correct a stored Kiosk Satellite password.
  The form verifies the replacement with the device before saving and keeps
  other device settings intact when authentication fails.

- Add a `rename_device` service that renames a device's Kiosk Satellite identity (name,
  hostname, ESPHome node name) via the authenticated `:2324` settings API, then updates the KSM
  entry's title/name and migrates its DNS host once the new hostname is verified to resolve to
  the same device. Returns a per-layer result rather than raising on a device-side failure;
  Android's own system device name has no verified non-ADB write path yet and is reported as
  `unsupported`.

- App updates and the `provision` service no longer use ADB after initial onboarding: updates
  and Update all now run over Kiosk Satellite's own authenticated `:2324` API, and `provision`
  applies settings with a `PATCH /api/settings` call instead of the `ks.provision` ADB intent.
  ADB is now used only for onboarding and the Install/Reinstall and Uninstall buttons.

- Add per-device diagnostics: detected device type, the install recipe the next install will
  run (or `none` with a reason), IP address from the device's health report, and an ADB
  enabled sensor that probes the ADB port every 5 minutes. Add an Auto-update all switch to
  the Configure KSM entry: while on, every device auto-updates as if its own switch were on.

- Add one Configure KSM entry for future-device defaults, a shared latest-release sensor,
  and an Update all button. Review mode preselects global settings; automatic mode waits
  for an ADB address, on-device authorization, and an action confirmation. Global edits
  leave existing devices unchanged. Update all checks reachability and Home Assistant's
  skipped-version choice, uses each device's verified install path, and reports outcomes.

- Add a Kiosk Satellite `update` entity per device. It checks GitHub releases hourly, shares one
  check across all devices, and installs through the Install button's verified path, with
  progress and release notes. Add an opt-in per-device auto-update switch. Installs on one
  device no longer overlap: a second install is refused while one is running.

- Restore automatic device-name and Home Assistant setup with released Kiosk Satellite's
  HTTP-only management API. Credential requests stay on the configured device and never
  follow redirects. The management network can observe the password and tokens.

- Decouple default-launcher selection from Kiosk Satellite's HTTP management API. Launcher-capable
  installs now enable the fixed Home alias over ADB, select it through Android's package manager,
  and fail unless the HOME resolver reads back Kiosk Satellite; no password or token is sent. Portal
  Go's observed Android 10 build keeps Meta's higher-priority resolver despite a successful selection
  command, so its v3 recipe and onboarding form explicitly omit launcher takeover while sibling
  models retain their independent launcher-capable assignment.

- Record Portal Go's exact-build secure-settings qualification failure: KS
  2026.9.70 does not declare the required permission. Scope failed qualification
  records by firmware/SDK before deriving support, keeping other builds and
  sibling models independent. Correct the physical matrix's negative permission
  control to distinguish a manifest declaration from a granted permission.

- Qualify Meta Portal Go's Test Harness recovery profile from an authorized
  live reset on the Test Portal. The reset remains explicitly consent-gated;
  KSM only reports the exact-model evidence and never executes it.
- Require an authenticated Home Assistant administrator or a user with
  control permission for a KSM device's Install button before any KSM
  device-management service opens ADB. Provisioning now accepts only a small,
  typed allowlist of supported settings.
- Stop KSM from sending a kiosk password, device token, or Home Assistant
  credential to Kiosk Satellite's HTTP-only management service. These
  credential-bearing calls now fail closed until the device offers verified
  HTTPS; unauthenticated status and health checks remain available.
- Fix KSM-owned Home Assistant credential cleanup on current Home Assistant:
  refresh-token removal is synchronous and no longer raises during entry
  removal.
- Stop automatic onboarding from issuing owner credentials: each kiosk now
  receives a dedicated local-only read-only user, a 90-day credential, and
  managed credential rotation on Install/Reinstall.
- Restore strict certificate validation on every Kiosk Satellite Home Assistant
  settings sync, explicitly clearing the legacy insecure browser setting.
- Reject malformed device HTTP responses through aiohttp's real parser within
  a bounded KSM client operation.
- Stop Portal provisioning from disabling Android's device-wide package
  verification setting.
- Protect KSM's shared ADB private key with private directory/file modes,
  ownership checks, symlink rejection, and serialized first-run generation.
- Fail closed when a Kiosk Satellite APK signer is not explicitly pinned in
  KSM's trusted policy, and never uninstall/retry after Android rejects an
  update for an incompatible signing certificate. This preserves certificate
  continuity and installed app data.
- Revoke KSM-generated Home Assistant credentials when setup fails, a flow is
  abandoned, or a KSM entry is removed or receives a replacement credential,
  while preserving selected shared tokens.
- Cover generic probe failures and unknown SDK, device-owner, and package-state
  evidence without leaking raw exception content or authorizing automatic work.
- Make post-install health polling stop without an unnecessary final delay,
  while covering token persistence and install recovery cleanup paths.
- Reject KSM service calls aimed at stale config entries before opening an ADB connection.
- Correct the legacy Test Harness probe's trace identifier so it does not
  collide with install/update verification coverage.
- Return the config flow's actionable `token_not_found` error when a selected
  long-lived access token is revoked between rendering and submission, without
  minting a credential or starting installation.
- Surface automatic Kiosk Satellite installation failures as persistent Home
  Assistant notifications while keeping onboarding best-effort and recoverable.
- Move ADB private-key loading to a worker thread so KSM service calls do not block Home Assistant's event loop.
- Block automatic Kiosk Satellite installation when the package-state probe is denied, errors, or
  missing, instead reporting that the package state is unknown.
- Refine the Kiosk Satellite Manager onboarding form with detected device metadata, Home Assistant's
  native name/area assignment, clearer default-launcher wording, and a secure existing-token picker.
- Record the physical PortalGo profile match (`Facebook` / `PortalGo` / Android 10, SDK 29)
  in a sanitized fixture and prevent it from silently regressing to a fallback profile.
- Add the response-only `kiosk_satellite_manager.onboarding_plan` service, which converts
  sanitized Android capability evidence into a deterministic, explainable dry-run plan without
  executing actions or authorizing destructive operations.
- Add the read-only `kiosk_satellite_manager.capability_report` response service for sanitized
  Android capability evidence.
