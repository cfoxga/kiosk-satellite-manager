# Changelog

## Unreleased

- Restore strict certificate validation on every Kiosk Satellite Home Assistant
  settings sync, explicitly clearing the legacy insecure browser setting.
- Reject malformed device HTTP responses through aiohttp's real parser within
  a bounded KSM client operation.
- Stop Portal provisioning from disabling Android's device-wide package
  verification setting.
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
