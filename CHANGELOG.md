# Changelog

## Unreleased

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
