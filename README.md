# Kiosk Satellite Manager

A Home Assistant custom integration that provisions [Kiosk Satellite](https://github.com/jxlarrea/kiosk-satellite)
devices over network ADB — installing the app, granting its permissions, and applying its settings
in one pass, without a cable or a factory-reset window. It replaces DroidMesh's provisioning role;
DroidMesh's peer-mesh and resident-agent features are retired, not ported, since Kiosk Satellite now
manages its own fleet natively.

**Status: pre-release.** No functional provisioning code yet — this repo currently carries only the
integration skeleton (config flow shell, manifest, empty setup/unload). See the design doc and phased
plan in the [ham-harness](https://git.cfoxga.com/cfoxga/ham-harness) repo's `kiosk-satellite-manager/`
silo (`docs/SPEC/provisioning.md`) for the full architecture and rollout phases.

Not an officially affiliated Kiosk Satellite product — "Manager" in the name is deliberate.

## Installation

Via [HACS](https://hacs.xyz/) as a custom repository (not yet listed in the default HACS store):
add `https://git.cfoxga.com/cfoxga/kiosk-satellite-manager` as an Integration-type custom repository,
then install **Kiosk Satellite Manager** and restart Home Assistant.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).
