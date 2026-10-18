from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bitrix_mcp.identity import BitrixIdentity, WebhookIdentity
from bitrix_mcp.oauth.identity import OAuthIdentity
from bitrix_mcp.oauth.store import TokenStore

if TYPE_CHECKING:
    from fastmcp.server.auth import AccessToken

    from bitrix_mcp.bitrix import BitrixClient


@dataclass
class ResolvedIdentity:
    identity: BitrixIdentity
    email: str | None
    mode: str  # webhook | oauth
    token_expires_at: int | None = None
    display_name: str | None = None
    bitrix_user_id: int | None = None


class IdentityResolver:
    """Map the authenticated MCP request onto a Bitrix24 identity.

    With OAuth on, ``BitrixTokenVerifier`` has already validated the upstream
    Bitrix token and put the user into ``AccessToken.claims``. Without OAuth
    (local development) every request runs as the inbound webhook.
    """

    def __init__(self, *, webhook_url: str, oauth_enabled: bool):
        self.oauth_enabled = oauth_enabled
        self._webhook = WebhookIdentity(webhook_url)

    def resolve(self, access_token: AccessToken | None) -> ResolvedIdentity:
        if not self.oauth_enabled or access_token is None:
            return ResolvedIdentity(identity=self._webhook, email=None, mode='webhook')
        identity = identity_from_access_token(access_token)
        if identity is None:
            return ResolvedIdentity(identity=self._webhook, email=None, mode='webhook')
        return ResolvedIdentity(
            identity=identity,
            email=identity.email,
            mode='oauth',
            token_expires_at=identity.expires_at or None,
            display_name=identity.display_name or None,
            bitrix_user_id=identity.bitrix_user_id,
        )


def identity_from_access_token(access_token: AccessToken) -> OAuthIdentity | None:
    claims = access_token.claims or {}
    user_id = claims.get('bitrix_user_id')
    endpoint = claims.get('client_endpoint')
    if not user_id or not endpoint:
        return None
    return OAuthIdentity(
        email=str(claims.get('email') or ''),
        member_id=str(claims.get('member_id') or ''),
        bitrix_user_id=int(user_id),
        client_endpoint=str(endpoint),
        access_token=access_token.token,
        expires_at=int(access_token.expires_at or 0),
        display_name=str(claims.get('name') or ''),
    )


def current_access_token() -> AccessToken | None:
    try:
        from fastmcp.server.dependencies import get_access_token
    except Exception:
        return None
    return get_access_token()


def identity_kind(identity: BitrixIdentity | None) -> str:
    if identity is None:
        return 'webhook'
    return getattr(identity, 'kind', 'webhook')


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
