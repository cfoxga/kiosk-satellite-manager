# Device Owner on Meta Portals without a factory reset

KSM can make Kiosk Satellite the Android **Device Owner** on supported Meta Portals over network
ADB. It does not need a factory reset, `adb shell cmd testharness enable`, root, or a USB cable.
This guide covers why that normally needs a wipe, how KSM avoids it, what it costs, and how KSM
keeps it safe.

The Android 10 procedure is proven on Portal Mini, Portal Go and Portal Gen 2. The Android 9
cleanup is proven on Portal Gen 1. Every other model, including Portal+ and Portal TV, is blocked
before KSM changes anything. Nothing here applies automatically to another device.

## What Device Owner gives Kiosk Satellite

Kiosk Satellite works without Device Owner. As Device Owner it adds:

- **Silent self-update:** no confirmation on the screen.
- **True lock-task kiosk:** no pin dialog or escape gesture.
- **Silent Home role:** it becomes the Home app without anyone choosing it.
- **Remote restart.**
- **Real Wi-Fi MAC address:** Android otherwise hides it.

## The Android rule

Android sets a Device Owner in two ways: during first-boot provisioning (NFC or QR code), or with
`dpm set-device-owner`. The command refuses while **any** account exists on the device:

```
java.lang.IllegalStateException: Not allowed to set the device owner because there are
already some accounts on the device
```

A Portal that has been set up always has four Meta account types: `com.facebook.aloha.sso`,
`com.facebook.aloha.pl` and `com.facebook.aloha.privowner` (the Meta login and owner identity), and
`com.facebook.aloha.hw` (hardware). Meta's setup wizard creates them with a network call the first
time the Portal gets Wi-Fi. They are not created at boot.

The checks run only when Device Owner is set. Accounts created afterward do not remove it. That's
why both methods below can put the Meta accounts back once Kiosk Satellite is Device Owner.

## The old method: Test Harness Mode

Before KSM's current method, the only reliable way to reach zero accounts was a full wipe with
`adb shell cmd testharness enable`:

1. The host running ADB must already be trusted by the Portal. Test Harness Mode keeps the trusted
   ADB key through the wipe. A host that wasn't trusted loses access, and only a physical factory
   reset recovers the device.
2. The wipe starts immediately and asks for **no** confirmation on the screen.
3. After the wipe the Portal must stay offline: no Wi-Fi and no on-screen setup until enrollment is
   done. Either one recreates the accounts and blocks `set-device-owner` again.
4. Install Kiosk Satellite, then run
   `dpm set-device-owner me.jxl.kiosk_satellite/.KioskAdminReceiver`.
5. Someone signs back in to Facebook or WhatsApp on the Portal.

This works, but it erases everything, it needs a cable and a careful offline window, and it
can't run from Home Assistant. KSM never runs it. It only reports, read-only, whether Test Harness
Mode is supported or active.

## The finding that removed the wipe

`dumpsys account` lists each account type with the package that registers it. On Portal Mini,
Portal Go and Portal Gen 2, **all four Meta account types come from one package**:
`com.facebook.alohaservices.alohausers`.

Android forgets an account as soon as its registering package is gone for that user. You can
remove a package for one user without deleting its APK, and restore it later:

```bash
adb shell pm uninstall -k --user 0 com.facebook.alohaservices.alohausers   # accounts gone within ~3 s
adb shell dumpsys account                                                   # confirm: no Account { rows
adb shell dpm set-device-owner me.jxl.kiosk_satellite/.KioskAdminReceiver
adb shell cmd package install-existing --user 0 com.facebook.alohaservices.alohausers
adb shell dumpsys device_policy | grep -A3 -i "device owner"               # package=me.jxl.kiosk_satellite
```

`install-existing` reinstalls the package from the APK still on the device. Device Owner stays
set while the package re-creates its accounts.

These two approaches do **not** clear the accounts:

- `pm disable-user` on the individual account components: the shell user isn't allowed to change
  them.
- `pm disable-user` on the whole package: the accounts stay registered.

`dpm get-device-owner` isn't reliable on these Android 10 builds. Check ownership by reading
`dumpsys device_policy`.

## The cost: the Meta login is erased

Removing `alohausers` keeps its APK, but **not its encrypted user data**. When the package is
restored, that data is recreated empty. This happens even if `set-device-owner` is never run:

- Only `com.facebook.aloha.hw` comes back. `sso`, `pl` and `privowner` are gone. The log shows
  `aloha.AlohaIdHelper: aloha id is null`.
- The Portal still thinks it is set up, but its Meta identity is gone. Facebook and WhatsApp login
  stop working. Add Profile asks an owner profile that no longer exists for approval, and Next does
  nothing.
- Left like this, app updates and later ADB access stopped working too, and only a factory reset
  recovered the device.
- Meta's setup app, `com.facebook.alohaapps.devicesetup`, turns itself off after the first setup,
  so it never runs again on its own. The shell can't turn it back on with `pm enable` ("Shell
  cannot change component state").

So the method has a price: **someone has to set up the Portal and sign in to Meta again, at the
device.**

## Restoring the Meta login without a reset

Removing and restoring Meta's setup app returns it to its first-run state:

```bash
adb shell pm uninstall -k --user 0 com.facebook.alohaapps.devicesetup
adb shell cmd package install-existing --user 0 com.facebook.alohaapps.devicesetup   # enabled again, data empty
adb shell am start -n com.facebook.alohaapps.devicesetup/com.facebook.aloha.app.devicesetup.DeviceSetupActivity
```

Someone then completes the setup wizard on the Portal. It starts at "Which language should Portal
use?". At the end they sign in with WhatsApp, which needs approval on the phone, or with Facebook.
Afterward all four account types are back, the Meta identity resolves, `user_setup_complete` is
`1`, ADB is still on, and Device Owner is still set.

**Kiosk Satellite's kiosk lock must be off while the wizard runs.** As Device Owner, Kiosk
Satellite allows only itself in lock task, so the wizard can't come to the front.

## The full manual sequence (Android 10)

KSM runs all of this for you (next section). The manual version is useful for understanding the
method or for diagnosing a device:

1. Connect over ADB and confirm Kiosk Satellite is installed (`pm path me.jxl.kiosk_satellite`).
2. `dumpsys device_policy`: there must be no Device Owner yet.
3. `pm list users`: there must be exactly one user.
4. `dumpsys account`: every account type must come from `com.facebook.alohaservices.alohausers`.
   An account from any other package means this method doesn't apply.
5. `pm uninstall -k --user 0 com.facebook.alohaservices.alohausers`, then read `dumpsys account`
   again until no accounts remain.
6. `dpm set-device-owner me.jxl.kiosk_satellite/.KioskAdminReceiver`.
7. `cmd package install-existing --user 0 com.facebook.alohaservices.alohausers`, and confirm with
   `pm list packages --user 0 com.facebook.alohaservices.alohausers`. **Always do this step, even if
   step 6 failed.**
8. Confirm `dumpsys device_policy` names `me.jxl.kiosk_satellite` as Device Owner.
9. Turn off Kiosk Satellite's kiosk lock, then reset and launch Meta setup (previous section).
10. Have someone finish setup and sign in on the Portal. Confirm `dumpsys account` shows the `sso`,
    `pl` and `privowner` types again, then turn the kiosk lock back on.

## How KSM does it

### Where to find it

- **Existing device:** the device's **Configure → Enable Device Owner (advanced)**.
- **New device:** tick **Enable Device Owner** on the ADB add-device form. It is off by default and
  runs after Kiosk Satellite is installed. The device is still added whatever the Device Owner
  result, and a notification says what happened. Device Owner never makes onboarding fail.

KSM never does any of this on its own.

### Read-only check first

KSM connects over the device's ADB address and reads, without changing anything:

- whether Kiosk Satellite is installed;
- the current Device Owner, from `dumpsys device_policy`;
- the number of Android users;
- each account type and the package that registers it, from `dumpsys account`;
- on Portals, which Meta identity types (`sso`, `pl`, `privowner`) are missing.

KSM never reads account names into the result or shows them. Only types and packages are used.

The flow stops with a reason, and changes nothing, if:

- ADB can't be reached;
- Kiosk Satellite or another app is already Device Owner;
- there is more than one user;
- Kiosk Satellite isn't installed;
- an install or update is running on the device;
- any account belongs to a package that isn't approved for that exact model;
- the device's state can't be read. KSM treats an unreadable result as a blocker, never as "no
  owner" or "no accounts".

### Confirmation

The form lists the benefits and side effects: which account types will be deleted, that the Meta
sign-in must be redone on the device, and that undoing Device Owner needs a factory reset. Nothing
happens until the confirmation box is ticked.

### Enrollment

- KSM removes only the packages approved for that exact model. Today that is
  `com.facebook.alohaservices.alohausers` on Portal Mini, Portal Go, Portal Gen 2 and Portal+
  Gen 2. Sharing an
  Android version or a recipe with a supported model doesn't make another model eligible. With zero
  accounts, no package is touched.
- KSM always restores every package it removed, even when a later step fails or the ADB connection
  drops. It confirms the restore with `pm list packages --user 0`. If it can't confirm, it reports
  `restore_failed` with the package name and the exact command to restore it by hand.
- KSM reports success only when `dumpsys device_policy` names Kiosk Satellite as Device Owner.
  The output of `set-device-owner` isn't trusted. If accounts remain or the command is refused, KSM
  stops and says so.
- KSM holds the device's install lock for the whole flow, so no install or update runs ADB against
  the device at the same time.

### Bringing Meta setup back

On Portal Mini, Portal Go, Portal Gen 2 and Portal+ Gen 2, enrollment always ends by putting Meta setup back on
screen:

1. KSM signs in to Kiosk Satellite's settings API with the device's password and turns off
   whichever of the kiosk lock and lockdown settings were on.
2. It resets and launches Meta's setup app as described above. It counts this as done only once
   the setup screen is in front.
3. A notification asks you to finish setup and sign in with Facebook or WhatsApp on the Portal.
4. A background watch checks `dumpsys account` every 15 seconds and reconnects if ADB drops.
   When all three Meta identity types are back, KSM turns the lock settings back on and updates the
   notification: login restored, kiosk mode back on or not, and whether ADB is still on.
5. The watch continues after a Home Assistant restart, keeping its original deadline. After 60
   minutes it stops and tells you how to try again. **KSM never turns the kiosk lock back on while
   the Meta login is still missing**, because the lock would hide the wizard.

If KSM can't show the setup screen, it turns the lock settings back on and reports
"Device Owner enabled, setup screen not shown: <reason>". Device Owner itself still succeeded. If
the device has no password stored or the settings API fails, KSM still shows setup and says it
couldn't turn kiosk mode off.

Running **Enable Device Owner** again on a Portal that is already Device Owner but missing its Meta
login offers **Show Meta setup** instead. This restarts the same setup step without touching Device
Owner.

## Keeping ADB working after Device Owner

ADB matters when you need it, for example to run Device Owner again, rerun an install recipe or
investigate a problem. How you get it back doesn't matter, as long as you have a way that works.
The following was verified on a factory-reset Portal+ (Gen 2, Android 10) enrolled through KSM.

- **KSM does not reboot the Portal.** The Meta setup screen that comes back looks like a fresh
  boot, but it isn't one. Wi-Fi stays configured, and ADB, including network ADB on port 5555,
  keeps working while you sign in again.
- **Network ADB does not survive a power cycle.** After a power off and on with no USB cable
  connected, port 5555 refused connections, and a later reboot did not bring it back.
  `adb tcpip 5555` sets a value that is cleared at boot (`persist.adb.tcp.port` stays empty).
  Treat network ADB as gone after any restart unless something turns it back on.
- **Ways to get it back.** Any of these works:
  1. Connect a USB cable from a trusted computer and run `adb tcpip 5555`.
  2. Leave a small USB host, such as a Raspberry Pi, attached permanently and have it run
     `adb tcpip 5555` whenever the Portal appears. Network ADB is back within seconds of every boot.
  3. Open Meta's **Settings → Debug** and switch **ADB Enabled** on. This needs the Meta sign-in.
     If port 5555 still refuses afterwards, run `adb tcpip 5555` over USB.
- **The Debug pane can be blank for a while after the Meta sign-in.** Meta's Settings app shows
  **ADB Enabled** only after a request to Meta's servers returns the owners allowed to use ADB. We
  have seen this take about 20 minutes, and we have seen it take under a second. Nothing on the
  device needs to change: wait, then reopen **Settings → Debug**. The setting Meta's code logs as
  `aloha_show_prod_adb_enabled_setting` is not what controls this.
- **Avoid power cycles you don't need while network ADB is your only way in.** That includes a
  reboot from Home Assistant or ESPHome. Set up one of the ways above first.
- **Kiosk Satellite itself doesn't need ADB.** Updates, settings and backups go through Kiosk
  Satellite's own API on port 2324 and keep working without ADB. KSM's ADB actions still need it:
  **Enable Device Owner**, **Show Meta setup**, the Portal Gen 1 cleanup, install recipes and
  uninstall.

## Portal Gen 1 (Android 9)

Portal Gen 1 uses a different, separately confirmed flow. Its confirmation says plainly that the
Meta sign-in will be erased, and it does not bring Meta setup back afterward. The device becomes a
dedicated Kiosk Satellite display.

Before changing anything, KSM requires all of the following:

- the device was identified as a Portal Gen 1, and it reads as Android 9 (SDK 28), manufacturer
  `Facebook`, model `Portal`, device `aloha`;
- one Android user and only approved account types;
- Kiosk Satellite installed, its own Home setting on, and already selected as Android Home;
- ADB on, network ADB on port 5555;
- the Meta packages the flow expects are present.

After you confirm, KSM:

1. Makes Kiosk Satellite Device Owner with the same remove, set and restore steps as Android 10.
2. Clears the data of a fixed list of Meta packages: users, personal user, state, launcher,
   Messenger, WhatsApp, contacts, feed and presence.
3. Disables a fixed list of Meta launcher and app-layer packages.

It never touches Meta Settings, Android's system or input packages, or anything ADB needs, and it
leaves Kiosk Satellite enabled.

KSM reports success only after fresh readbacks show:

- zero accounts;
- Kiosk Satellite as Device Owner and as Android Home;
- every disabled package listed as disabled;
- ADB on, port 5555, and a working ADB shell.

If any check fails, KSM reports a partial cleanup. Network ADB has not been tested across a reboot on
Gen 1. Expect it to behave like Android 10 and be gone after a restart (see
[Keeping ADB working after Device Owner](#keeping-adb-working-after-device-owner)).

Portal+ Gen 1 shares the Android 9 install recipe, but it has not been qualified for this cleanup
and is blocked.

## Supported models

| Model | Android | Device Owner path |
|---|---|---|
| Portal Mini | 10 | Account clear + Meta setup |
| Portal Go | 10 | Account clear + Meta setup |
| Portal Gen 2 | 10 | Account clear + Meta setup |
| Portal Gen 1 | 9 | Gen 1 cleanup (Meta login not restored) |
| Portal+ Gen 1, Portal+ Gen 2, Portal TV | 9 / 10 | Blocked: not qualified |
| Any other device | — | Blocked unless no accounts exist on it |

## Undoing it

Android has no way to hand Device Owner from Kiosk Satellite to another app. Removing Kiosk
Satellite as owner takes these steps, the same ones KSM's uninstall button uses:

```bash
adb shell dpm remove-active-admin --user 0 me.jxl.kiosk_satellite/.KioskAdminReceiver
adb shell pm disable-user --user 0 me.jxl.kiosk_satellite
adb shell pm clear me.jxl.kiosk_satellite
adb shell pm uninstall me.jxl.kiosk_satellite
```

A plain `pm uninstall` of a Device Owner app fails with `DELETE_FAILED_DEVICE_POLICY_MANAGER`.
Confirm the removal with `pm path`, not by the command's exit code. A factory reset clears Device
Owner and everything else.

## Approaches that don't work

- **Copying Meta's data from a healthy Portal.** The shell can't read the app data directories.
  `run-as` is refused because the packages aren't debuggable, backup is disallowed, and there is
  no root.
- **Meta's scripted-login receiver.** `com.facebook.aloha.debug.adboobe.LOGIN` accepts Wi-Fi,
  device name and sign-in details, but it is protected by a signature permission
  (`com.facebook.aloha.permission.ADB_OOBE_DEBUG`). The shell's broadcast is refused.
- **Disabling instead of removing.** See [The finding that removed the wipe](#the-finding-that-removed-the-wipe).
- **Long manual sessions over network ADB.** Portal network ADB on port 5555 closes on its own
  after a while, without a reboot. Keep the connection busy, for example with an idle
  `adb shell` loop.

## Limitations

- Evidence is per exact model. A Portal model not listed above is blocked even if its accounts look
  the same.
- Setting up Meta again and signing in to Facebook or WhatsApp is always done by a person at the
  Portal. KSM can't do it.
- Removing Device Owner needs either the uninstall sequence above or a factory reset. KSM can't
  give Device Owner back to Meta or hand it to another app.
- If you skip the Meta sign-in, Kiosk Satellite keeps working, but the Portal's own "Enable ADB"
  setting can't be switched back on later from Meta's settings.
- Network ADB is lost on every power cycle, and KSM can't turn it back on. See
  [Keeping ADB working after Device Owner](#keeping-adb-working-after-device-owner).
