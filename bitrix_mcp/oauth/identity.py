from __future__ import annotations

import hashlib
from typing import Any, Callable, Awaitable
from urllib.parse import urlparse


class OAuthIdentity:
    """User OAuth identity. Access token is never included in repr/str."""

    __slots__ = (
        'email',
        'member_id',
        'bitrix_user_id',
        'client_endpoint',
        '_access_token',
        '_token_fingerprint',
        '_on_auth_error',
        'display_name',
        'expires_at',
    )

    kind = 'oauth'

    def __init__(
        self,
        *,
        email: str,
        member_id: str,
        bitrix_user_id: int,
        client_endpoint: str,
        access_token: str,
        expires_at: int,
        display_name: str = '',
        on_auth_error: Callable[[], Awaitable[bool]] | None = None,
    ):
        endpoint = client_endpoint.rstrip('/') + '/'
        parsed = urlparse(endpoint)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError('client_endpoint must be an absolute HTTP(S) URL.')
        self.email = email
        self.member_id = member_id
        self.bitrix_user_id = int(bitrix_user_id)
        self.client_endpoint = endpoint
        self._access_token = access_token
        self._token_fingerprint = hashlib.sha256(access_token.encode('utf-8')).hexdigest()[:12]
        self._on_auth_error = on_auth_error
        self.display_name = display_name
        self.expires_at = int(expires_at)

    @property
    def cache_key(self) -> str:
        return f'{self.member_id}|{self.bitrix_user_id}'

    @property
    def label(self) -> str:
        return self.email

    def request_url(self, method: str) -> str:
        method = method.strip().lstrip('/')
        if not method:
            raise ValueError('Bitrix24 method name is empty.')
        if not method.endswith('.json'):
            method = f'{method}.json'
        return f'{self.client_endpoint}{method}'

    def auth_params(self) -> dict[str, Any]:
        return {'auth': self._access_token}

    async def on_auth_error(self) -> bool:
        if self._on_auth_error is None:
            return False
        return await self._on_auth_error()

    def replace_access_token(self, access_token: str, *, expires_at: int) -> None:
        self._access_token = access_token
        self._token_fingerprint = hashlib.sha256(access_token.encode('utf-8')).hexdigest()[:12]
        self.expires_at = int(expires_at)

    def __repr__(self) -> str:
        return (
            f'OAuthIdentity(email={self.email!r}, user_id={self.bitrix_user_id}, '
            f'cache_key={self.cache_key!r}, token_fp={self._token_fingerprint!r})'
        )

    def __str__(self) -> str:
        return self.email
