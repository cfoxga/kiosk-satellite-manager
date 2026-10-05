"""KSM-TEST-394/395/396: dedicated kiosk users (KSM-BEHAVE-199/200)."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState

from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_READ_ONLY, GROUP_ID_USER
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, Credentials
from homeassistant.auth.permissions.const import POLICY_CONTROL
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.kiosk_satellite_manager.credentials import (
    TokenCredential,
    async_revoke_owned_credential,
)
from custom_components.kiosk_satellite_manager.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_MANAGER
from custom_components.kiosk_satellite_manager.install import _mint_ha_token

from .conftest import init_integration


async def _token(hass, user, client_name: str):
    return await hass.auth.async_create_refresh_token(
        user,
        client_name=client_name,
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=90),
    )


def _owned(refresh_token) -> TokenCredential:
    return TokenCredential(access_token="unused", refresh_token_id=refresh_token.id, owned=True)


async def _seed_owner(hass):
    """HA makes the first non-system user the owner; take that slot first."""
    owner = MockUser(name="Owner", is_owner=True).add_to_hass(hass)
    owner.local_only = True
    return owner


async def test_minted_kiosk_user_may_control_entities(hass):
    """[KSM-TEST-394] A minted kiosk user is a local-only, non-admin member of
    Users and may control an entity it has never been told about."""
    await _seed_owner(hass)
    credential = await _mint_ha_token(hass, "Kiosk Satellite Manager - Kitchen [abc]")

    user = hass.auth.async_get_refresh_token(credential.refresh_token_id).user
    assert user.name == "Kiosk Satellite - Kitchen"
    assert [group.id for group in user.groups] == [GROUP_ID_USER]
    assert user.local_only is True
    assert user.is_admin is False
    assert user.permissions.check_entity("select.kitchen_wake_word", POLICY_CONTROL) is True


async def test_revoking_an_owned_token_removes_its_kiosk_user(hass):
    """[KSM-TEST-395] Revocation removes the dedicated user once it holds no
    token, and never removes the owner or a kiosk user with a token left."""
    owner = await _seed_owner(hass)
    owner_token = await _token(hass, owner, "legacy owned owner token")
    kiosk = await _mint_ha_token(hass, "Kiosk Satellite Manager - Kitchen")
    kiosk_user_id = hass.auth.async_get_refresh_token(kiosk.refresh_token_id).user.id
    shared = await _mint_ha_token(hass, "Kiosk Satellite Manager - Office")
    shared_user = hass.auth.async_get_refresh_token(shared.refresh_token_id).user
    await _token(hass, shared_user, "second token")

    await async_revoke_owned_credential(hass, kiosk)
    await async_revoke_owned_credential(hass, _owned(owner_token))
    await async_revoke_owned_credential(hass, shared)

    assert hass.auth.async_get_refresh_token(kiosk.refresh_token_id) is None
    assert await hass.auth.async_get_user(kiosk_user_id) is None
    assert hass.auth.async_get_refresh_token(owner_token.id) is None
    assert await hass.auth.async_get_user(owner.id) is not None
    assert hass.auth.async_get_refresh_token(shared.refresh_token_id) is None
    assert await hass.auth.async_get_user(shared_user.id) is not None


async def test_manager_setup_reconciles_kiosk_users(hass):
    """[KSM-TEST-396] Manager setup moves a token-holding Read Only kiosk user
    to Users, removes a tokenless one, and touches nothing else."""
    await _seed_owner(hass)
    live = await hass.auth.async_create_user(
        "Kiosk Satellite - Kitchen", group_ids=[GROUP_ID_READ_ONLY], local_only=True
    )
    live_token = await _token(hass, live, "Kiosk Satellite Manager - Kitchen")
    orphan = await hass.auth.async_create_user(
        "Kiosk Satellite - Test Portal Go", group_ids=[GROUP_ID_READ_ONLY], local_only=True
    )
    with_login = await hass.auth.async_create_user(
        "Kiosk Satellite - Hand made", group_ids=[GROUP_ID_READ_ONLY], local_only=True
    )
    with_login.credentials.append(
        Credentials(auth_provider_type="homeassistant", auth_provider_id=None, data={"username": "hand"})
    )
    admin = await hass.auth.async_create_user(
        "Kiosk Satellite - Admin", group_ids=[GROUP_ID_ADMIN], local_only=True
    )
    system = MockUser(name="Kiosk Satellite - System", system_generated=True).add_to_hass(hass)
    system.local_only = True
    remote = await hass.auth.async_create_user(
        "Kiosk Satellite - Remote", group_ids=[GROUP_ID_READ_ONLY], local_only=False
    )
    other = await hass.auth.async_create_user("Guest", group_ids=[GROUP_ID_READ_ONLY], local_only=True)
    inactive_admin = await hass.auth.async_create_user(
        "Kiosk Satellite - Retired admin", group_ids=[GROUP_ID_ADMIN], local_only=True
    )
    await hass.auth.async_deactivate_user(inactive_admin)

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})):
        await init_integration(hass)

    moved = await hass.auth.async_get_user(live.id)
    assert [group.id for group in moved.groups] == [GROUP_ID_USER]
    assert hass.auth.async_get_refresh_token(live_token.id) is not None
    assert await hass.auth.async_get_user(orphan.id) is None
    for untouched, group_id in (
        (with_login, GROUP_ID_READ_ONLY),
        (admin, GROUP_ID_ADMIN),
        (remote, GROUP_ID_READ_ONLY),
        (other, GROUP_ID_READ_ONLY),
        (inactive_admin, GROUP_ID_ADMIN),
    ):
        user = await hass.auth.async_get_user(untouched.id)
        assert user is not None, untouched.name
        assert [group.id for group in user.groups] == [group_id], untouched.name
    assert await hass.auth.async_get_user(system.id) is not None


async def test_reconcile_failure_never_blocks_manager_setup(hass):
    """[KSM-TEST-396] negative: a user HA refuses to remove is logged and
    skipped; the manager entry still loads and the other users still move."""
    await _seed_owner(hass)
    orphan = await hass.auth.async_create_user(
        "Kiosk Satellite - Orphan", group_ids=[GROUP_ID_READ_ONLY], local_only=True
    )
    live = await hass.auth.async_create_user(
        "Kiosk Satellite - Kitchen", group_ids=[GROUP_ID_READ_ONLY], local_only=True
    )
    await _token(hass, live, "Kiosk Satellite Manager - Kitchen")

    with patch("custom_components.kiosk_satellite_manager.fetch_health", new=AsyncMock(return_value={})), \
            patch.object(hass.auth, "async_remove_user", new=AsyncMock(side_effect=KeyError("gone"))):
        await init_integration(hass)

    manager = next(
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_MANAGER
    )
    assert manager.state is ConfigEntryState.LOADED
    assert await hass.auth.async_get_user(orphan.id) is not None
    moved = await hass.auth.async_get_user(live.id)
    assert [group.id for group in moved.groups] == [GROUP_ID_USER]


async def test_failed_mint_removes_the_new_user(hass):
    """[KSM-TEST-395] negative: a token that can't be created leaves no user."""
    await _seed_owner(hass)
    before = {user.id for user in await hass.auth.async_get_users()}
    with patch.object(
        hass.auth, "async_create_refresh_token", new=AsyncMock(side_effect=ValueError("refused"))
    ), pytest.raises(ValueError):
        await _mint_ha_token(hass, "Kiosk Satellite Manager - Kitchen")
    assert {user.id for user in await hass.auth.async_get_users()} == before
