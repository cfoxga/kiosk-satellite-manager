# Changelog

## Unreleased

- Portals no longer show a black screen when their built-in public DNS server answers for
  your Home Assistant name. Install turns Android Private DNS off (unless you chose a specific
  Private DNS hostname) and Uninstall puts the earlier setting back. Install on every device
  now raises a repair when the device resolves the dashboard address to a public address that
  Home Assistant does not use. Portals set up earlier: press **Install Kiosk Satellite** once,
  or set Private DNS to Off on the device. The Portal recipe keys now end in `_local_dns` (#121).
- Meta Portal Go and Portal+ Gen 2 are now **supported** on their tested Android 10
  builds. Install no longer asks for the secure-settings permission, which Kiosk
  Satellite never requests, so permission setup no longer fails on every Portal.
  The Portal recipe keys gain a `_declared_grants` suffix (#120).
- KSM installs through HACS as a custom repository from
  `github.com/cfoxga/kiosk-satellite-manager`. The README gives the HACS and
  manual steps, Home Assistant's Documentation and issue links open GitHub, and
  a GitHub workflow runs hassfest and HACS validation (#118).

## 0.4.0

- README covers every shipped surface: the device Configure menu, Replace
  launcher, Enable ESPHome, configuration backup and restore, the Permissions
  sensor and Fix permissions, diagnostics, Discovered followers and the
  `onboarding_plan` service. It no longer lists a global home launcher setting,
  and the install recipes page says Replace launcher sets Home (#117).

- Adding a device with ESPHome on now also turns on Kiosk Satellite's
  **ESPHome entities** switch, so Home Assistant gets the device's entities
  instead of an empty ESPHome device. When the device is already in ESPHome,
  KSM reloads that entry after turning entities on so they appear (#116).

- The device catalog now carries qualification evidence for the Meta Portal+
  Gen 2 on its tested Android 10 build. Install, update, reinstall and uninstall
  passed, but that build reports **blocked** because Kiosk Satellite 2026.10.2
  does not request the secure-settings permission the Portal recipe requires (#115).
- Meta setup recovery now saves its pending login watch on the device entry,
  resumes it after a Home Assistant restart, and allows 60 minutes for sign-in
  before showing the manual retry instructions (#79).
- Install now logs Kiosk Satellite's reason when its Home Assistant connection
  check fails, with provisioning credentials redacted, so the device's failure
  can be diagnosed without a generic warning alone (#63).
- **Install Kiosk Satellite** no longer fails with a server error while GitHub is
  rate limiting KSM. It installs the release from the last successful update check,
  like auto-update and Update all; with no successful check yet it says the release
  lookup failed and suggests pinning a downloaded version (#107).
- The first start after the fleet migration no longer logs a `KeyError` from
  sensor setup for a device that moved while its new parent was still loading;
  that device now gets its entities when the parent reloads (#80).
- When KSM creates a fleet for a Fleet Manager, each follower on that leader's
  roster that KSM doesn't manage yet appears under Discovered. Confirming a card
  checks the kiosk and asks for its Kiosk Satellite password. A follower that
  joins an existing fleet later raises a repair asking whether to add it, just
  once. Fleets that already existed record their current roster without
  prompting. KSM still never changes fleet membership (#111).
- The physical-device test matrix accepts `--allow-missing-ha-entry` so it can run on a Home Assistant with no entry for the test Portal, leaving none behind (#52).
- The Portal Gen 1 cleanup confirmation now shows its title, explanation and
  checkbox label when opened from a device's Configure menu; it was blank (#108).
- **Uninstall Kiosk Satellite** is now a Configuration control, so it no longer
  sits among a device's everyday controls (#108).
- Device-reported component names are shell-quoted before KSM passes them to
  `settings put secure` and `cmd notification allow_listener` (#108).
- The manifest declares the `adb_shell` logger, so its log level can be set with
  the integration's own. `hacs.json` now requires Home Assistant 2025.3, the
  first release with the config subentries KSM uses (#108).
- README describes the current update surface (ESPHome's update entity, the
  Auto-update switch, Install version and Hide follower updates) instead of the
  removed update entity and version sensor (#108).

## 0.3.0

- Point Home Assistant's KSM Documentation link directly to the supported
  device catalog, which links to the install recipe reference.

- Add a per-device **Replace launcher** option (#105). When on, Install
  enables KS's home screen through its settings API, selects it as the Android
  Home activity, and reads the resolver back; a miss raises a notification and
  the install still completes. Unset defaults to on for Portal recipes and off
  for Android TV / Onn devices, so existing devices need no migration.
  Configure it from the device's Configure menu. Turning it off later does not
  revert Home.

- Add native KSM Settings diagnostic entities for the exact device catalog and
  install recipes (#104). Per-device Install recipe diagnostics now show typed
  settings and operations. New operator pages document model assignments,
  qualification limits, and recipe behavior; the integration documentation
  link now points to OneDev.

- Add a per-device **Permissions** problem sensor and **Fix permissions** button
  (#102). The sensor reads the device's own permission, AppOp and battery
  exemption state every 15 minutes and lists what is not granted; the button
  re-grants and re-reads. Permissions the installed KS does not declare are
  listed as `not_declared` and never raise the problem. Also fixes the AppOp
  readback misreading Android's `allow; time=…` output as not granted.

- Rename the manager entry and device to **KSM Settings** and add **Backup All**
  (#101). The button saves each loaded device's configuration through its
  existing backup path and reports failures without stopping other backups.

- Fix Notification Access convergence on all recipes (#98). Install now grants
  KS's declared listener through Android's notification command and verifies
  that the service actually binds, instead of trusting a secure-setting entry
  that can appear granted while the listener remains inactive.

- Restore configuration no longer undoes itself on a second click (#100). The
  safety backup taken before a restore is now named `..._pre-restore_...`,
  stays listed and selectable, but is never the default: the Configuration
  backup select and Restore with nothing chosen use the newest regular
  backup. A safety copy equal to the newest backup is not written.

- Fix Install on existing GTV entries created before their exact model had a
  recipe (#99). An explicit press now checks live ADB identity, uses only an
  approved exact recipe, and saves the model after a successful install.
  Unknown devices still fail closed before any device change.

- Recognize Android 14 onn 4K Pro devices by their live-confirmed model and
  assign a dedicated provisioning recipe (#98). All recipes converge KS
  Notification Access when the installed app declares its listener service;
  the new recipe includes Nearby devices, Modify system settings, and All
  files access grants.

- Android 9 Portal Gen 1 now has a separate `meta_portal_android9` recipe and
  a confirmed cleanup flow (#97). It enrolls Kiosk Satellite as Device Owner,
  clears Meta accounts and app data, disables audited Meta launcher/services,
  and checks native KS Home and network ADB. Android 10 Go, Mini and Gen 2
  models share `meta_portal_android10`; Portal TV remains separate.

- New global setting **Hide follower updates** (#95). While on, KSM disables the ESPHome update
  entity of every confirmed fleet follower, so a Kiosk Satellite release prompts once per fleet
  leader instead of once per device. KSM re-enables only entities it disabled itself, when the
  setting goes off or a device becomes a leader.
- KSM no longer creates a **Kiosk Satellite version** sensor or an **Update** entity (#95); the
  ESPHome update entity covers both. KSM removes those two entities from existing devices at
  startup. Auto-update, Update all and the Install version pin are unchanged.

- Every add form has an **Enable ESPHome** checkbox that defaults to the manager's
  Enable ESPHome option (formerly "Turn on ESPHome on new devices") (#96). When ticked, the wizard turns
  ESPHome on in Kiosk Satellite, waits for its encryption key and for Home Assistant to discover the
  device, adds it with that key, and waits for it to load. Any failure raises a notification (never
  showing the key) and the device is still added to KSM.

- KSM no longer offers a Home launcher toggle or changes `home.enabled` or Android's
  default Home app during install and updates (#94). Configure Home in Kiosk
  Satellite itself. Portal recipe keys have no version numbers.

- A device's Configure menu has **Change host** (#93). Enter a DNS name or IP address; KSM
  checks that Kiosk Satellite answers there under the device's saved key and changes nothing
  otherwise, so a name that now points at a reverse proxy is refused rather than trusted. Use
  it to point KSM at a device's IP when its DNS name moves to a proxy that fronts the web UI.

- The Add device form can select an existing Kiosk Satellite Fleet (#92). KSM asks its leader
  to invite the new kiosk after KS is running and its local admin connection is ready, before
  writing Home Assistant settings. Accept the invitation on the new kiosk; HA placement follows
  confirmed KS membership. A failed invitation leaves the added device under Unmanaged and
  displays a notification.

- Explicitly renaming a Meta Portal from KSM Configure now also updates its
  Android **Portal Name** over one temporary ADB connection (#81). Enable
  network ADB on the Portal for the action; KSM verifies the Android setting
  and reports it incomplete if ADB is unavailable. Automatic rename paths do
  not use ADB.

- KSM now groups kiosks under native Home Assistant fleet entries (#77). It creates an Unmanaged
  entry and a separate entry for each confirmed Kiosk Satellite leader, moves accepted members as
  they join or leave, and shows leader, managed count, reachability and sync status. Existing
  device entries migrate to subentries while retaining their entity IDs, device IDs, settings,
  service target IDs and backup paths. KSM only reads fleet status; invitations and sync remain
  controlled on the kiosks.

- Each KSM device's Configure menu now offers **Rename device** (#78). It uses
  KSM's existing rename operation and shows which name layers completed,
  which need attention, and any old ESPHome action callers.

- Enable Device Owner now supports Portal Gen 2 (#54). The Great Room Portal
  confirmed the same account-clearing and Device Owner sequence as Portal Go
  and Mini; KSM reopens Meta setup for the required on-device sign-in afterward.

- Meta Portal updates that fail because Android's package verifier rejects the
  APK now try once more after KSM reads and disables that verifier over ADB
  (#76). Other devices and failures are unaffected. Manual update failures
  now write a WARNING with the device's error to Home Assistant's log.

- KSM checks for new Kiosk Satellite releases every 15 minutes (#75). When it
  detects a new version, including its first successful check, it asks every
  loaded device to refresh its update status. A device that loads later also
  checks after setup, so its ESPHome update entity can see that release.

- The global **Install version** list now always offers at least five versions
  (#74): every downloaded version, marked "(downloaded)", plus the newest
  Kiosk Satellite releases. Picking one that isn't downloaded yet downloads it
  the first time a kiosk installs it.
- KSM is back to downloading the per-CPU Kiosk Satellite APK, and only for the
  CPU types your kiosks actually have (#74, replacing #71's single universal
  APK). A universal APK KSM already downloaded is still used; it is only
  downloaded again for a kiosk whose CPU type can't be read.

- KSM no longer manages Voice Satellite (#73). Kiosk Satellite 2026.9.87 and
  later has voice built in through its own ESPHome server, so the separate
  Voice Satellite integration isn't needed. KSM no longer creates or reuses a
  Voice Satellite entry for each kiosk, no longer offers to install Voice
  Satellite through HACS, and no longer updates a kiosk's satellite entity
  when one is renamed. Voice Satellite entries KSM created earlier are left in
  place; remove them once each kiosk has migrated from its own Voice
  Satellite settings page.

- The manager's options have a new **Install version** setting (#72). **Latest**
  (the default) follows new releases as before. Picking a version KSM has already
  downloaded makes every kiosk's update, Update all, auto-update and the ADB
  Install use that version instead, so you can test an upgrade without waiting
  for a new release. Kiosks only move forward: to re-test an upgrade on a kiosk
  that is already newest, Uninstall it, pick the older version and ADB Install,
  then switch back to Latest.

- **Enable Device Owner no longer leaves a Portal without its Meta login**
  (#54). Enabling Device Owner has to remove the Portal's accounts, which also
  signed it out of Meta and FB/WhatsApp with no way back short of a factory
  reset. On a Portal Mini or Portal Go, KSM now turns kiosk mode off, brings
  up Meta's setup screen, and tells you to finish setup and sign in
  with Facebook or WhatsApp on the Portal. When the login is back, KSM turns kiosk mode on again and
  updates the notification. A Portal that already lost its login gets a
  **Show Meta setup** step under Configure → Enable Device Owner. Portal Go
  can now enable Device Owner too.

- KSM now keeps only the universal Kiosk Satellite APK, which works on every
  kiosk, instead of one file per CPU type (#71). That is one ~195 MB file per
  version, however many kinds of kiosk you have.

- Every managed kiosk now gets an ESPHome node name (#67). If the kiosk has
  none, or only Kiosk Satellite's generated `kiosk-satellite-xxxxxx`, KSM sets
  it from the device's name (Great Room Portal → `great-room-portal`) the next
  time the device loads; a node name you chose is left alone. KSM does not turn ESPHome on by itself: the manager's
  options have a new **Turn on ESPHome on new devices** setting, off by
  default. When it is on, a kiosk you add has ESPHome turned on once. After
  that, your choice on the kiosk stands.

- KSM now downloads each Kiosk Satellite release **once**, keeps it on the
  Home Assistant server, and sends it to your kiosks itself (#70). The update
  entity, auto-update and **Update all** all upload KSM's copy over the
  kiosk's own API, so kiosks no longer each fetch the release from GitHub.
  The Install/Reinstall button uses the same copy. Every copy is checked
  against KSM's trusted signer before it is kept. Old copies are cleaned up
  automatically. KSM keeps the version each kiosk runs, the release just
  before the oldest of those, and the newest three. The copies live in
  `/config/.cache`, which backups skip.

- Each kiosk can now back up and restore its Kiosk Satellite configuration
  (#69). **Back up configuration** saves the kiosk's full settings to a dated
  file under `config/kiosk_satellite_manager/backups/`. Backups also run
  automatically every 24 hours by default; set the period, or 0 to turn them
  off, in the Kiosk Satellite Manager entry's options. The same options set
  how many backups to keep per kiosk (default 10). A backup identical to the
  previous one replaces it, so every kept file is different. To restore, pick
  a file in **Configuration backup** and press **Restore configuration**. KSM
  first backs up the current settings, then keeps the kiosk's current
  password and Home Assistant token so the restore can't lock KSM out. The
  files contain the kiosk's passwords and token, so treat them as secrets.
  They can also be imported through Kiosk Satellite's own Settings page.

- **Update all** now updates every eligible kiosk at the same time instead of
  one after another (#68). A kiosk that fails doesn't stop or cancel the
  others, and the summary notification still arrives once they have all
  finished.

- The Kiosk Satellite Manager entry now has a **Check for updates** button
  (#66). Pressing it checks GitHub for a new Kiosk Satellite release right
  away instead of waiting for the hourly check. Whenever KSM sees a release it
  hadn't seen before, it tells every managed kiosk to check GitHub too, so each
  kiosk sees the release within the hour instead of on its own twice-a-day
  check. Nothing is installed automatically unless auto-update is on.

- Renaming a Kiosk Satellite device from Home Assistant or HAM now carries
  through to the kiosk (#64). HAM asks KSM to rename the device, and KSM
  pushes the new name, hostname and ESPHome node name to Kiosk Satellite. When
  the Voice Satellite entity is renamed, KSM points Kiosk Satellite's
  `ha.satellite_entity` at the new ID so voice keeps working.

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
