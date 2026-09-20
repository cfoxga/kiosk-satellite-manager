# Changelog

## Unreleased

- Move ADB private-key loading to a worker thread so KSM service calls do not block Home Assistant's event loop.
- Block automatic Kiosk Satellite installation when the package-state probe is denied, errors, or
  missing, instead reporting that the package state is unknown.
- Add the response-only `kiosk_satellite_manager.onboarding_plan` service, which converts
  sanitized Android capability evidence into a deterministic, explainable dry-run plan without
  executing actions or authorizing destructive operations.
- Add the read-only `kiosk_satellite_manager.capability_report` response service for sanitized
  Android capability evidence.
