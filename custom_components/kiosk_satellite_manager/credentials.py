"""KSM credential ownership helpers."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_READ_ONLY, GROUP_ID_USER

from .const import CONF_HA_REFRESH_TOKEN_ID, CONF_HA_TOKEN, CONF_HA_TOKEN_OWNED, DOMAIN

_LOGGER = logging.getLogger(__name__)

KIOSK_USER_PREFIX = "Kiosk Satellite - "
_KIOSK_USER_LOCK = f"{DOMAIN}_kiosk_user_lock"


def kiosk_user_lock(hass) -> asyncio.Lock:
    """Serialize minting with cleanup, so cleanup never removes a user whose
    token is still being created (KSM-BEHAVE-200)."""
    return hass.data.setdefault(_KIOSK_USER_LOCK, asyncio.Lock())


def is_dedicated_kiosk_user(user) -> bool:
    """A user `_mint_ha_token` created for one kiosk (KSM-BEHAVE-199): no
    login, local-only, and never the owner, an admin, or system-generated."""
    return (
        (user.name or "").startswith(KIOSK_USER_PREFIX)
        and user.local_only
        and not user.credentials
        and not user.is_owner
        # Group membership, not `is_admin`, which reads False for an inactive admin.
        and not any(group.id == GROUP_ID_ADMIN for group in user.groups)
        and not user.system_generated
    )


@dataclass(frozen=True)
class TokenCredential:
    access_token: str
    refresh_token_id: str | None
    owned: bool

    @classmethod
    def from_entry_data(cls, data: dict) -> "TokenCredential | None":
        """Read an entry credential conservatively.

        Entries created before ownership metadata must never be guessed to be
        KSM-owned: deleting a user's still-needed token is worse than leaving a
        legacy token for the operator to revoke explicitly.
        """
        access_token = data.get(CONF_HA_TOKEN)
        if not access_token:
            return None
        return cls(
            access_token=access_token,
            refresh_token_id=data.get(CONF_HA_REFRESH_TOKEN_ID),
            owned=bool(data.get(CONF_HA_TOKEN_OWNED, False)),
        )

    def as_entry_data(self) -> dict:
        """Return the persisted representation without exposing auth objects."""
        data = {CONF_HA_TOKEN: self.access_token}
        if self.refresh_token_id is not None:
            data[CONF_HA_REFRESH_TOKEN_ID] = self.refresh_token_id
        if self.owned:
            data[CONF_HA_TOKEN_OWNED] = True
        return data


async def async_revoke_owned_credential(hass, credential: TokenCredential | None) -> None:
    """Revoke a KSM-owned credential when its lifecycle ends."""
    if credential is None or not credential.owned or not credential.refresh_token_id:
        return
    refresh_token = hass.auth.async_get_refresh_token(credential.refresh_token_id)
    if refresh_token is None:
        return
    user = refresh_token.user
    hass.auth.async_remove_refresh_token(refresh_token)
    # KSM-BEHAVE-200: the dedicated user exists only for this token.
    if is_dedicated_kiosk_user(user) and not user.refresh_tokens:
        await hass.auth.async_remove_user(user)


async def async_reconcile_kiosk_users(hass) -> None:
    """Move Read Only kiosk users to Users (KSM-BEHAVE-199) and remove the
    tokenless ones earlier revocations left behind (KSM-BEHAVE-200)."""
    async with kiosk_user_lock(hass):
        for user in await hass.auth.async_get_users():
            if not is_dedicated_kiosk_user(user):
                continue
            # One user's failure must never block manager setup or the rest.
            try:
                if not user.refresh_tokens:
                    await hass.auth.async_remove_user(user)
                elif any(group.id == GROUP_ID_READ_ONLY for group in user.groups):
                    await hass.auth.async_update_user(user, group_ids=[GROUP_ID_USER])
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Could not reconcile kiosk user %s", user.id, exc_info=True)


async def async_replace_entry_credential(hass, entry, credential: TokenCredential) -> None:
    """Replace the credential stored on an entry."""
    previous = TokenCredential.from_entry_data(entry.data)
    if previous is not None and previous.refresh_token_id != credential.refresh_token_id:
        await async_revoke_owned_credential(hass, previous)
    data = {
        **entry.data,
        **credential.as_entry_data(),
    }
    # Do not carry stale ownership metadata across a replacement with a
    # deliberately reused token.
    if not credential.owned:
        data.pop(CONF_HA_TOKEN_OWNED, None)
    if credential.refresh_token_id is None:
        data.pop(CONF_HA_REFRESH_TOKEN_ID, None)
    from . import fleet
    fleet.update_device(hass, entry, data=data)
