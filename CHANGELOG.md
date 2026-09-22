# Changelog

## Unreleased

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
