"""OAuth 2.1 authorization server for MCP clients, backed by a Bitrix24 application.

MCP clients (Claude connectors, etc.) need DCR + PKCE + RFC 8414 metadata, which
Bitrix24 OAuth does not offer. FastMCP's ``OAuthProxy`` provides all of that
locally and forwards the user to the portal's ``/oauth/authorize/``. This module
plugs Bitrix24 specifics into it:

- ``BitrixTokenClient`` talks to ``oauth.bitrix.info`` (GET with query params,
  non-standard response fields) and pins the portal ``member_id``.
- ``BitrixTokenVerifier`` validates an upstream access token with ``user.current``
  and exposes the Bitrix user in ``AccessToken.claims``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken, OAuthProxy, TokenVerifier
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from bitrix_mcp.cache import TTLCache
from bitrix_mcp.oauth.store import TokenStore

BITRIX_TOKEN_URL = 'https://oauth.bitrix.info/oauth/token/'
CALLBACK_PATH = '/oauth/callback'
# Bitrix24 refresh tokens live 28 days and are not reported in the token response.
REFRESH_TOKEN_TTL_SECONDS = 28 * 24 * 3600
# Short, so an expired token stops validating soon and the proxy refreshes it.
VERIFY_CACHE_SECONDS = 60


class BitrixOAuthError(RuntimeError):
    pass


class BitrixTokenClient:
    """Stand-in for authlib's ``AsyncOAuth2Client`` used by ``OAuthProxy``.

    Only ``fetch_token`` and ``refresh_token`` are called by the proxy.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        store: TokenStore,
        token_url: str = BITRIX_TOKEN_URL,
        timeout: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.store = store
        self.token_url = token_url
        self.timeout = timeout
        self.transport = transport

    async def fetch_token(self, url: str | None = None, *, code: str, **_: Any) -> dict[str, Any]:
        return await self._request({'grant_type': 'authorization_code', 'code': code})

    async def refresh_token(self, url: str | None = None, *, refresh_token: str, **_: Any) -> dict[str, Any]:
        return await self._request({'grant_type': 'refresh_token', 'refresh_token': refresh_token})

    async def _request(self, params: dict[str, str]) -> dict[str, Any]:
        query = {'client_id': self.client_id, 'client_secret': self.client_secret, **params}
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.get(self.token_url, params=query)
        try:
            data = response.json()
        except ValueError as exc:
            raise BitrixOAuthError(f'Bitrix24 OAuth returned non-JSON response (HTTP {response.status_code}).') from exc
        if not isinstance(data, dict) or data.get('error') or response.status_code >= 400:
            error = data.get('error') if isinstance(data, dict) else None
            description = data.get('error_description') if isinstance(data, dict) else None
            raise BitrixOAuthError(f'Bitrix24 OAuth error: {error or response.status_code} {description or ""}'.strip())
        if not data.get('access_token') or not data.get('refresh_token'):
            raise BitrixOAuthError('Bitrix24 OAuth response is missing access_token or refresh_token.')

        member_id = str(data.get('member_id') or '').strip()
        if not await asyncio.to_thread(self.store.pin_or_check_member_id, member_id):
            raise BitrixOAuthError('Bitrix24 member_id does not match the portal linked to this server.')
        return normalize_token_response(data)


def normalize_token_response(data: dict[str, Any]) -> dict[str, Any]:
    """Map a Bitrix24 token response onto the fields ``OAuthProxy`` expects."""
    token = dict(data)
    # Bitrix scopes ("crm,task,user") are application permissions, not OAuth
    # scopes the MCP client asked for; keep them out of the proxy's scope checks.
    if 'scope' in token:
        token['bitrix_scope'] = token.pop('scope')
    if 'expires_in' not in token and token.get('expires'):
        token['expires_in'] = max(int(token['expires']) - int(time.time()), 60)
    token.setdefault('token_type', 'Bearer')
    return token


class BitrixTokenVerifier(TokenVerifier):
    """Validate a Bitrix24 user access token via ``user.current`` on the configured portal."""

    def __init__(
        self,
        *,
        portal_url: str,
        store: TokenStore | None = None,
        timeout: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_seconds: int = VERIFY_CACHE_SECONDS,
    ):
        super().__init__()
        self.client_endpoint = portal_url.rstrip('/') + '/rest/'
        self.store = store
        self.timeout = timeout
        self.transport = transport
        self._cache = TTLCache(ttl_seconds=cache_seconds)

    async def verify_token(self, token: str) -> AccessToken | None:
        key = hashlib.sha256(token.encode('utf-8')).hexdigest()
        cached = self._cache.get(key)
        if isinstance(cached, AccessToken):
            return cached

        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.get(f'{self.client_endpoint}user.current.json', params={'auth': token})
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if response.status_code >= 400 or not isinstance(data, dict) or data.get('error'):
            return None
        profile = data.get('result')
        if not isinstance(profile, dict) or not profile.get('ID'):
            return None

        member_id = ''
        if self.store is not None:
            member_id = await asyncio.to_thread(self.store.get_pinned_member_id) or ''
        name = ' '.join(
            part for part in (str(profile.get('NAME') or '').strip(), str(profile.get('LAST_NAME') or '').strip()) if part
        )
        access = AccessToken(
            token=token,
            client_id=str(profile['ID']),
            scopes=[],
            claims={
                'bitrix_user_id': int(profile['ID']),
                'email': TokenStore.normalize_email(str(profile.get('EMAIL') or '')),
                'name': name,
                'member_id': member_id,
                'client_endpoint': self.client_endpoint,
            },
        )
        self._cache.set(key, access)
        return access


class BitrixOAuthProvider(OAuthProxy):
    def __init__(
        self,
        *,
        portal_url: str,
        client_id: str,
        client_secret: str,
        base_url: str,
        store: TokenStore,
        storage_dir: str | Path,
        storage_encryption_key: str,
        jwt_signing_key: str | None = None,
        allowed_client_redirect_uris: list[str] | None = None,
        timeout: float = 20,
    ):
        self._bitrix_client_id = client_id
        self._bitrix_client_secret = client_secret
        self._bitrix_store = store
        self._bitrix_timeout = timeout
        super().__init__(
            upstream_authorization_endpoint=f'{portal_url.rstrip("/")}/oauth/authorize/',
            upstream_token_endpoint=BITRIX_TOKEN_URL,
            upstream_client_id=client_id,
            upstream_client_secret=client_secret,
            token_verifier=BitrixTokenVerifier(portal_url=portal_url, store=store, timeout=timeout),
            base_url=base_url,
            redirect_path=CALLBACK_PATH,
            allowed_client_redirect_uris=allowed_client_redirect_uris,
            forward_pkce=False,  # Bitrix24 does not support PKCE; the proxy still enforces it for MCP clients.
            forward_resource=False,
            client_storage=encrypted_file_storage(storage_dir, storage_encryption_key),
            jwt_signing_key=jwt_signing_key or None,
            fallback_refresh_token_expiry_seconds=REFRESH_TOKEN_TTL_SECONDS,
        )

    def _create_upstream_oauth_client(self) -> BitrixTokenClient:  # type: ignore[override]
        return BitrixTokenClient(
            client_id=self._bitrix_client_id,
            client_secret=self._bitrix_client_secret,
            store=self._bitrix_store,
            timeout=self._bitrix_timeout,
        )


def encrypted_file_storage(storage_dir: str | Path, encryption_key: str) -> FernetEncryptionWrapper:
    """Persistent store for proxy state (client registrations, upstream tokens)."""
    directory = Path(storage_dir)
    directory.mkdir(parents=True, exist_ok=True)
    file_store = FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
    )
    return FernetEncryptionWrapper(
        key_value=file_store,
        fernet=Fernet(encryption_key.encode('utf-8')),
        raise_on_decryption_error=False,
    )
