"""Scripted Portal ADB shell shared by the #54 Device Owner tests
(KSM-TEST-167..170). The dumpsys shapes are the live test-portal-mini output
(2026-09-25), with account names replaced by a sentinel the tests assert
never leaks."""
from __future__ import annotations

META = "com.facebook.alohaservices.alohausers"
KS_ADMIN = "me.jxl.kiosk_satellite/.KioskAdminReceiver"
SECRET = "person@example.com"

_AUTH = (
    "  RegisteredServicesCache: 4 services\n"
    "    ServiceInfo: AuthenticatorDescription {type=com.facebook.aloha.hw}, "
    f"ComponentInfo{{{META}/com.facebook.aloha.system.alohausers.services2.HWAuthenticatorService}}, uid 10048\n"
    "    ServiceInfo: AuthenticatorDescription {type=com.facebook.aloha.pl}, "
    f"ComponentInfo{{{META}/com.facebook.aloha.system.alohausers.services2.PLAuthenticatorService}}, uid 10048\n"
    "    ServiceInfo: AuthenticatorDescription {type=com.facebook.aloha.privowner}, "
    f"ComponentInfo{{{META}/com.facebook.aloha.system.alohausers.services2.PrivOwnerAccountsAuthenticatorService}}, uid 10048\n"
    "    ServiceInfo: AuthenticatorDescription {type=com.facebook.aloha.sso}, "
    f"ComponentInfo{{{META}/com.facebook.aloha.system.alohausers.services.AuthenticationService}}, uid 10048\n"
    "    ServiceInfo: AuthenticatorDescription {type=com.example.other}, "
    "ComponentInfo{com.example.other/com.example.other.Auth}, uid 10099\n"
)
_META_TYPES = ("pl", "sso", "hw", "privowner")


def _accounts(types) -> str:
    rows = "".join(
        f"    Account {{name={SECRET}, type={t}}}\n" for t in types
    )
    return f"User UserInfo{{0:Owner:13}}:\n  Accounts: {len(types)}\n{rows}\n{_AUTH}"


def _policy(owner_pkg: str | None) -> str:
    base = (
        "Current Device Policy Manager state:\n"
        "  Enabled Device Admins (User 0, provisioningState: 0):\n"
        f"    {KS_ADMIN}:\n      testOnlyAdmin=false\n"
    )
    if owner_pkg is None:
        return base
    return (
        "Current Device Policy Manager state:\n"
        "  Device Owner: \n"
        f"    admin=ComponentInfo{{{owner_pkg}/{owner_pkg}.Receiver}}\n"
        "    name=\n"
        f"    package={owner_pkg}\n"
        "  Device Owner Type: -1\n"
    ) + base[len("Current Device Policy Manager state:\n"):]


class FakeDevice:
    """A scripted Portal: state changes only through the commands KSM sends."""

    def __init__(
        self,
        *,
        account_types=tuple(f"com.facebook.aloha.{t}" for t in _META_TYPES),
        owner=None,
        users=1,
        ks_installed=True,
        uninstall_purges=True,
        set_owner_output="Success: Device owner set to package me.jxl.kiosk_satellite",
        set_owner_takes_effect=True,
        restore_works=True,
    ):
        self.account_types = list(account_types)
        self.owner = owner
        self.users = users
        self.ks_installed = ks_installed
        self.uninstall_purges = uninstall_purges
        self.set_owner_output = set_owner_output
        self.set_owner_takes_effect = set_owner_takes_effect
        self.restore_works = restore_works
        self.meta_installed = True
        self.commands: list[str] = []

    async def shell(self, command: str) -> str:
        self.commands.append(command)
        if command == "dumpsys account":
            return _accounts(self.account_types)
        if command == "dumpsys device_policy":
            return _policy(self.owner)
        if command == "pm list users":
            rows = "".join(
                f"\tUserInfo{{{i}:User{i}:13}} running\n" for i in range(self.users)
            )
            return f"Users:\n{rows}"
        if command == "pm path me.jxl.kiosk_satellite":
            return "package:/data/app/ks/base.apk\n" if self.ks_installed else ""
        if command == f"pm uninstall -k --user 0 {META}":
            self.meta_installed = False
            if self.uninstall_purges:
                self.account_types = [
                    t for t in self.account_types if not t.startswith("com.facebook.aloha.")
                ]
            return "Success\n"
        if command == f"dpm set-device-owner {KS_ADMIN}":
            if self.set_owner_takes_effect:
                self.owner = "me.jxl.kiosk_satellite"
            return self.set_owner_output
        if command == f"cmd package install-existing --user 0 {META}":
            if self.restore_works:
                self.meta_installed = True
            return f"Package {META} installed for user: 0\n"
        if command == f"pm list packages --user 0 {META}":
            return f"package:{META}\n" if self.meta_installed else ""
        raise AssertionError(f"unexpected command {command!r}")

    def mutations(self) -> list[str]:
        return [
            c for c in self.commands
            if c.startswith(("pm uninstall", "dpm set", "cmd package install"))
        ]
