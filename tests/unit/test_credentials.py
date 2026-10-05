"""Credential ownership and revocation tests for KSM-managed HA tokens."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.kiosk_satellite_manager.credentials import (
    TokenCredential,
    async_replace_entry_credential,
    async_revoke_owned_credential,
)

# An owner whose legacy token was marked owned: never removed (KSM-BEHAVE-200).
_OWNER = SimpleNamespace(
    name="Owner", local_only=False, credentials=[], is_owner=True, is_admin=True,
    groups=[], system_generated=False, refresh_tokens={},
)


@pytest.mark.asyncio
async def test_owned_credential_is_revoked_once_and_missing_token_is_idempotent():
    """[KSM-TEST-100] Removal revokes only an extant KSM-owned credential."""
    refresh_token = SimpleNamespace(id="owned-refresh", user=_OWNER)
    hass = SimpleNamespace(auth=MagicMock())
    hass.auth.async_get_refresh_token.return_value = refresh_token
    hass.auth.async_remove_refresh_token = MagicMock()

    credential = TokenCredential("access", "owned-refresh", owned=True)
    await async_revoke_owned_credential(hass, credential)
    hass.auth.async_get_refresh_token.return_value = None
    await async_revoke_owned_credential(hass, credential)

    hass.auth.async_remove_refresh_token.assert_called_once_with(refresh_token)


@pytest.mark.asyncio
async def test_ksm_test_112_owned_credential_uses_home_assistants_sync_remover():
    """[KSM-TEST-112] HA removes refresh tokens synchronously."""
    refresh_token = SimpleNamespace(id="owned-refresh", user=_OWNER)
    hass = SimpleNamespace(auth=MagicMock())
    hass.auth.async_get_refresh_token.return_value = refresh_token
    hass.auth.async_remove_refresh_token = MagicMock()

    await async_revoke_owned_credential(hass, TokenCredential("access", "owned-refresh", owned=True))

    hass.auth.async_remove_refresh_token.assert_called_once_with(refresh_token)


@pytest.mark.asyncio
async def test_shared_or_legacy_credential_is_never_revoked():
    """[KSM-TEST-103] Reused and metadata-less tokens fail closed."""
    hass = SimpleNamespace(auth=MagicMock())
    hass.auth.async_remove_refresh_token = MagicMock()

    await async_revoke_owned_credential(hass, TokenCredential("shared", "shared-id", owned=False))
    await async_revoke_owned_credential(hass, TokenCredential("legacy", None, owned=True))

    hass.auth.async_get_refresh_token.assert_not_called()
    hass.auth.async_remove_refresh_token.assert_not_called()


@pytest.mark.asyncio
async def test_replacement_revokes_distinct_old_owned_credential_before_persisting_new():
    """[KSM-TEST-101] Replacement cannot leave KSM's prior token live."""
    refresh_token = SimpleNamespace(id="old-refresh", user=_OWNER)
    entry = SimpleNamespace(data={"ha_token": "old", "ha_refresh_token_id": "old-refresh", "ha_token_owned": True})
    hass = SimpleNamespace(auth=MagicMock(), config_entries=MagicMock())
    hass.auth.async_get_refresh_token.return_value = refresh_token
    hass.auth.async_remove_refresh_token = MagicMock()

    await async_replace_entry_credential(
        hass,
        entry,
        TokenCredential("new", "new-refresh", owned=True),
    )

    hass.auth.async_remove_refresh_token.assert_called_once_with(refresh_token)
    hass.config_entries.async_update_entry.assert_called_once_with(
        entry,
        data={"ha_token": "new", "ha_refresh_token_id": "new-refresh", "ha_token_owned": True},
    )
