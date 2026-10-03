# Install recipes and settings

Open **Settings → Devices & services → Kiosk Satellite Manager → KSM Settings
→ Diagnostic → Install recipes → Menu → Details** to see every source recipe, its assigned models,
and its typed settings. The state is the recipe count. A physical kiosk's
**Install recipe** diagnostic entity shows the recipe KSM would execute for that
device. `none` means KSM refuses installation for the stored model; it does not
fall back to a similar-looking recipe. These attributes describe **KSM's intended
install behavior**, not the device's current live Kiosk Satellite settings.

## Shared behavior

All five recipes install an ABI-matched Kiosk Satellite release, preserve an
already matching version, reinstall for a version mismatch, uninstall before an
explicit reinstall, and use `pm_uninstall` for removal. They install and start
the app, grant runtime permissions and AppOps appropriate to Android's SDK,
converge consent services, exempt battery optimization, sync their declared KS
parameters, and verify health. Postconditions include the target installed and
health-reported version, permission and AppOp readback, and battery exemption
readback. The native entity lists the exact operations and postconditions.

`permission_policy` and `appops_policy` are policy names, not a fixed permission
list: Android version and the installed app's declared components affect the
actual grants. KSM grants common microphone, camera, location and log access;
newer Android versions add Bluetooth and notification/media grants, while older
versions use external-storage grants. Notification Access is converged only when the
installed KS build declares its listener. KSM verifies the applicable grants
on the device during installation.

## Recipe-specific settings

| Recipe | Assigned models | Name source | Portal URL | Device Admin | Other behavior |
|---|---|---|---|---|---|
| `meta_portal_android10_verifier_off` | Go, Mini, Gen 2, Plus Gen 2 | Android secure Bluetooth name | `/portal` | Set | Portal permission/AppOps policy; Private DNS Off; package verifier Off (restored on Uninstall); retry a verifier failure after checking the verifier setting |
| `meta_portal_android9_verifier_off` | Gen 1, Plus Gen 1 | Android secure Bluetooth name | `/portal` | Set | Portal policy, Private DNS Off, package verifier Off, confirmed Android 9 Meta cleanup, verifier retry |
| `meta_portal_tv_verifier_off` | Portal TV | Android secure Bluetooth name | `/portal` | Set | Portal policy, Private DNS Off, package verifier Off and verifier retry; cleanup is unqualified |
| `onn_4k_pro_android14` | onn 4K Pro Android 14 | Android global device name | none | Not set | Standard Android TV permission/AppOps policy |
| `android_tv` | **None** | Android global device name | none | Not set | Generic standard TV behavior exists in source but cannot install on any model without an approved assignment |

### Private DNS and the dashboard address

Portal OS adds a public DNS server to the ones your network hands out, and Android's default
*Automatic* Private DNS prefers it. If your Home Assistant name resolves to a local address only
on your own DNS, the Portal gets the public one instead and Kiosk Satellite shows a black
screen. Install on a Portal therefore sets **Settings → Network → Private DNS** to **Off**, unless
you chose a specific Private DNS hostname, which KSM leaves alone. KSM remembers the earlier
setting and puts it back when you press **Uninstall Kiosk Satellite**. A Portal set up before
this change gets the fix the next time you press **Install Kiosk Satellite**, or set Private DNS
to Off on the device yourself.

On every device, Install also compares the address the device gets for the dashboard host with
Home Assistant's own answer. When the device cannot resolve it, or gets a public address where
Home Assistant gets only private ones, KSM raises a **dashboard address resolves differently**
repair that says what to fix. It clears on the next Install once the answers agree.

Every current recipe declares `browser.ignore_ssl_errors: true` as its KS
parameter. This does **not** override Home Assistant's pinned HTTPS connection
to the Kiosk Satellite management API. Recipes do not themselves set Kiosk
Satellite's native Home preference: the per-device **Replace launcher** option
does (on by default for the Portal recipes, off for the others), turning on KS's
home screen and selecting it as Android's Home app during Install. The Portal
recipes remove Android's redundant model suffix from the device name when present.

Recipes contain audited operation identifiers and typed parameters, never free
form shell commands. Qualification belongs to the exact model, recipe and Android
build, so sharing a recipe never transfers a support claim. See the
[device catalog](supported-devices.md) for model assignments and limitations.
