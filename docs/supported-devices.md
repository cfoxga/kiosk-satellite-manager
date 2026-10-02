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
| Meta Portal Go | `portal_go` | `meta_portal_android10_declared_grants` | Supported on the tested SDK 29 build (2026-10-02); other builds need their own evidence |
| Meta Portal Mini | `portal_mini` | `meta_portal_android10_declared_grants` | Assignment does not inherit Portal Go's evidence |
| Meta Portal Gen 1 | `portal_gen1` | `meta_portal_android9_declared_grants` | Android 9 recipe |
| Meta Portal Gen 2 | `portal_gen2` | `meta_portal_android10_declared_grants` | Android 10 recipe |
| Meta Portal+ Gen 1 | `portal_plus_gen1` | `meta_portal_android9_declared_grants` | Android 9 recipe |
| Meta Portal+ Gen 2 | `portal_plus_gen2` | `meta_portal_android10_declared_grants` | Supported on the tested SDK 29 build (2026-10-02); other builds need their own evidence |
| Meta Portal TV | `portal_tv` | `meta_portal_tv_declared_grants` | Cleanup remains unqualified |

The source catalog may evolve with a KSM update; the native entity always reads
the installed catalog. For a specific device and build, the sanitized
`kiosk_satellite_manager.capability_report` and `onboarding_plan` services give
the current support state and reason. Those states distinguish `recipe_assigned`,
`partially_qualified`, `supported`, `revalidation_required`, and `blocked`.

See [install recipes](install-recipes.md) for the settings behind each recipe.
