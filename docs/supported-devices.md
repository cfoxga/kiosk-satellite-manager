# Device catalog and qualification

KSM identifies an **exact model** before it selects an install recipe. An approved
recipe assignment permits KSM to attempt provisioning; it is **not** a claim that
every Android build of that model has passed physical qualification. An unknown
model, including a generic `gtv_stick` classification, has no executable recipe.

In Home Assistant, open **Settings → Devices & services → Kiosk Satellite Manager
→ KSM Settings → Diagnostic → Device catalog → Menu → Details**. Its state is the number of exact
models; its `models` attribute lists each model key, name, approved recipe key,
and number of qualification records for that assignment. Those counts say what
evidence is on file, not whether a particular firmware build is supported. The
device's **Device type** and **Install recipe** diagnostic entities show the
identity and executable recipe stored for that particular device.

| Exact model | Model key | Approved recipe | Qualification note |
|---|---|---|---|
| onn 4K Pro, Android 14 | `onn_4k_pro_android14` | `onn_4k_pro_android14` | Assigned; qualification is build-specific |
| Meta Portal Go | `portal_go` | `meta_portal_android10_local_dns` | Supported on the tested SDK 29 build (2026-10-02); other builds need their own evidence |
| Meta Portal Mini | `portal_mini` | `meta_portal_android10_local_dns` | Assignment does not inherit Portal Go's evidence |
| Meta Portal Gen 1 | `portal_gen1` | `meta_portal_android9_local_dns` | Android 9 recipe |
| Meta Portal Gen 2 | `portal_gen2` | `meta_portal_android10_local_dns` | Android 10 recipe |
| Meta Portal+ Gen 1 | `portal_plus_gen1` | `meta_portal_android9_local_dns` | Android 9 recipe |
| Meta Portal+ Gen 2 | `portal_plus_gen2` | `meta_portal_android10_local_dns` | Supported on the tested SDK 29 build (2026-10-02); other builds need their own evidence |
| Meta Portal TV | `portal_tv` | `meta_portal_tv_local_dns` | Cleanup remains unqualified |

The source catalog may evolve with a KSM update; the native entity always reads
the installed catalog. For a specific device and build, the sanitized
`kiosk_satellite_manager.capability_report` and `onboarding_plan` services give
the current support state and reason. Those states distinguish `recipe_assigned`,
`partially_qualified`, `supported`, `revalidation_required`, and `blocked`.

See [install recipes](install-recipes.md) for the settings behind each recipe.

## Requesting support for a device

A device that is not in the library, or a supported model on an untested build,
can be proposed for the library from Home Assistant. Nothing is installed or
approved by a request; KSM keeps refusing the device until a tested library entry
ships in a KSM update.

- **A device KSM added but cannot install on** raises a repair, *device not in Kiosk
  Satellite Manager's library* (Settings → System → Repairs). Submit it to read the
  device's identity over ADB, then open the pre-filled GitHub issue it links.
- **A device automatic onboarding refuses** shows the same link in the refusal.
- **Any device**, including a supported model on a new build: call the
  `kiosk_satellite_manager.support_request` action. It returns the request and the link.

The request proposes a new library entry (an exact-model match rule built from the
manufacturer, model, codename and Android SDK the device reported) and the closest
existing install recipe, if one fits. It carries the sanitized capability report,
never the device's address, name, accounts or credentials. When the report makes the
link too long, attach the device's **Download diagnostics**, which carries the whole
request.
