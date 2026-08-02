from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bitrix_mcp.identity import BitrixIdentity, WebhookIdentity
from bitrix_mcp.oauth.identity import OAuthIdentity
from bitrix_mcp.oauth.store import TokenStore

if TYPE_CHECKING:
    from bitrix_mcp.bitrix import BitrixClient
    from bitrix_mcp.oauth.flow import OAuthFlow
    from bitrix_mcp.oauth.refresh import RefreshCoordinator


@dataclass
class ResolvedIdentity:
    identity: BitrixIdentity
    email: str | None
    mode: str  # webhook | oauth
    token_expires_at: int | None = None
    display_name: str | None = None
    bitrix_user_id: int | None = None


class IdentityResolver:
    def __init__(
        self,
        *,
        webhook_url: str,
        store: TokenStore | None,
        refresh: RefreshCoordinator | None,
        oauth_enabled: bool,
    ):
        self.webhook_url = webhook_url
        self.store = store
        self.refresh = refresh
        self.oauth_enabled = oauth_enabled
        self._webhook = WebhookIdentity(webhook_url)

    async def resolve(self, email: str | None) -> ResolvedIdentity:
        if not self.oauth_enabled or not email or self.store is None or self.refresh is None:
            return ResolvedIdentity(identity=self._webhook, email=email, mode='webhook')

        email_n = TokenStore.normalize_email(email)
        token = await asyncio.to_thread(self.store.get_by_email, email_n)
        if token is None:
            return ResolvedIdentity(identity=self._webhook, email=email_n, mode='webhook')

        try:
            token = await self.refresh.ensure_fresh(token)
        except Exception:
            return ResolvedIdentity(identity=self._webhook, email=email_n, mode='webhook')

        identity = self._oauth_identity_from_token(token)

        async def _on_auth_error() -> bool:
            try:
                refreshed = await self.refresh.refresh(token)
            except Exception:
                return False
            identity.replace_access_token(refreshed.access_token, expires_at=refreshed.expires_at)
            return True

        identity._on_auth_error = _on_auth_error  # noqa: SLF001 - wire after construction
        return ResolvedIdentity(
            identity=identity,
            email=email_n,
            mode='oauth',
            token_expires_at=token.expires_at,
            bitrix_user_id=token.bitrix_user_id,
        )

    def _oauth_identity_from_token(self, token) -> OAuthIdentity:
        return OAuthIdentity(
            email=token.email,
            member_id=token.member_id,
            bitrix_user_id=token.bitrix_user_id,
            client_endpoint=token.client_endpoint,
            access_token=token.access_token,
            expires_at=token.expires_at,
        )


def identity_kind(identity: BitrixIdentity | None) -> str:
    if identity is None:
        return 'webhook'
    return getattr(identity, 'kind', 'webhook')


def extract_request_email(header_name: str) -> str | None:
    try:
        from fastmcp.server.dependencies import get_http_request
    except Exception:
        return None
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    raw = request.headers.get(header_name) or request.headers.get(header_name.lower())
    if not raw:
        return None
    value = TokenStore.normalize_email(str(raw))
    return value or None


async def fetch_user_profile(bitrix: BitrixClient) -> dict[str, Any]:
    result = await bitrix.call_method('user.current', {})
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return result[0]
    return {}


def profile_email(profile: dict[str, Any]) -> str:
    return TokenStore.normalize_email(str(profile.get('EMAIL') or profile.get('email') or ''))


def profile_display_name(profile: dict[str, Any]) -> str:
    parts = [
        str(profile.get('NAME') or '').strip(),
        str(profile.get('LAST_NAME') or '').strip(),
    ]
    name = ' '.join(part for part in parts if part)
    return name or str(profile.get('EMAIL') or profile.get('ID') or '')
