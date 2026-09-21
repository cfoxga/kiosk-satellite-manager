"""KSM credential ownership helpers."""
from __future__ import annotations

from dataclasses import dataclass

from .const import CONF_HA_REFRESH_TOKEN_ID, CONF_HA_TOKEN, CONF_HA_TOKEN_OWNED


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
    if refresh_token is not None:
        hass.auth.async_remove_refresh_token(refresh_token)


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
    hass.config_entries.async_update_entry(entry, data=data)
