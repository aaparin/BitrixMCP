from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from b24pysdk import BitrixApp

from bitrix_mcp.oauth.store import StoredToken, TokenStore
from bitrix_mcp.oauth.wait import AuthWaitRegistry


@dataclass(frozen=True)
class OAuthTokenPayload:
    access_token: str
    refresh_token: str
    expires_at: int
    user_id: int
    member_id: str
    client_endpoint: str
    scope: str


class OAuthFlow:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        portal_url: str,
        member_id: str,
        store: TokenStore,
        wait_registry: AuthWaitRegistry,
        auth_state_ttl_seconds: int = 900,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.portal_url = portal_url.rstrip('/')
        self.member_id = member_id
        self.store = store
        self.wait_registry = wait_registry
        self.auth_state_ttl_seconds = auth_state_ttl_seconds
        self._app = BitrixApp(client_id=client_id, client_secret=client_secret)

    def build_authorize_url(self, state: str) -> str:
        query = urlencode({'client_id': self.client_id, 'state': state})
        return f'{self.portal_url}/oauth/authorize/?{query}'

    async def begin_authorization(self, email: str) -> tuple[str, str]:
        """Create one-time state and return (state, authorize_url)."""
        state = secrets.token_urlsafe(32)
        await asyncio.to_thread(
            self.store.create_auth_state,
            state,
            email,
            ttl_seconds=self.auth_state_ttl_seconds,
        )
        await self.wait_registry.register(state)
        return state, self.build_authorize_url(state)

    async def exchange_code(self, code: str) -> OAuthTokenPayload:
        renewed = await asyncio.to_thread(self._app.get_oauth_token, code)
        token = renewed.oauth_token
        if not token.refresh_token:
            raise RuntimeError('Bitrix24 OAuth response did not include a refresh_token.')
        expires_at = int(token.expires.timestamp()) if token.expires else int(time.time()) + int(token.expires_in or 3600)
        scope = ','.join(renewed.scope) if isinstance(renewed.scope, list) else str(renewed.scope or '')
        return OAuthTokenPayload(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=expires_at,
            user_id=int(renewed.user_id),
            member_id=str(renewed.member_id),
            client_endpoint=str(renewed.client_endpoint).rstrip('/') + '/',
            scope=scope,
        )

    async def refresh(self, refresh_token: str) -> OAuthTokenPayload:
        renewed = await asyncio.to_thread(self._app.refresh_oauth_token, refresh_token)
        token = renewed.oauth_token
        if not token.refresh_token:
            raise RuntimeError('Bitrix24 refresh response did not include a refresh_token.')
        expires_at = int(token.expires.timestamp()) if token.expires else int(time.time()) + int(token.expires_in or 3600)
        scope = ','.join(renewed.scope) if isinstance(renewed.scope, list) else str(renewed.scope or '')
        return OAuthTokenPayload(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=expires_at,
            user_id=int(renewed.user_id),
            member_id=str(renewed.member_id),
            client_endpoint=str(renewed.client_endpoint).rstrip('/') + '/',
            scope=scope,
        )

    async def save_verified_token(self, *, email: str, payload: OAuthTokenPayload) -> StoredToken:
        return await asyncio.to_thread(
            self.store.save_token,
            member_id=payload.member_id,
            bitrix_user_id=payload.user_id,
            email=email,
            access_token=payload.access_token,
            refresh_token=payload.refresh_token,
            expires_at=payload.expires_at,
            client_endpoint=payload.client_endpoint,
            scope=payload.scope,
            status='active',
        )

    @staticmethod
    def emails_match(left: str, right: str) -> bool:
        return TokenStore.normalize_email(left) == TokenStore.normalize_email(right)

    def authorization_required_payload(
        self,
        *,
        authorize_url: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        return {
            'error': 'authorization_required',
            'message': message
            or 'Authorization was not completed. Open the link and retry the request.',
            'authorizeUrl': authorize_url,
            'expiresInSeconds': self.auth_state_ttl_seconds,
        }
