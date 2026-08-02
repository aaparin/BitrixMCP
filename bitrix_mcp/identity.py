from __future__ import annotations

import hashlib
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse


@runtime_checkable
class BitrixIdentity(Protocol):
    @property
    def cache_key(self) -> str: ...

    @property
    def label(self) -> str: ...

    def request_url(self, method: str) -> str: ...

    def auth_params(self) -> dict[str, Any]: ...

    async def on_auth_error(self) -> bool: ...


class WebhookIdentity:
    """Inbound webhook identity. Token stays in the URL path, never in auth_params."""

    __slots__ = ('base_url', '_cache_key', '_label')

    def __init__(self, webhook_url: str):
        base = webhook_url.rstrip('/') + '/'
        parsed = urlparse(base)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError('BITRIX_WEBHOOK_URL must be an absolute HTTP(S) URL.')

        path = parsed.path.rstrip('/')
        # Typical shape: /rest/<user_id>/<token>/
        segments = [part for part in path.split('/') if part]
        token = segments[-1] if segments else ''
        user = segments[-2] if len(segments) >= 2 else ''
        fingerprint = hashlib.sha256(token.encode('utf-8')).hexdigest()[:12]

        self.base_url = base
        self._cache_key = f'{parsed.netloc}|{user}|{fingerprint}'
        self._label = f'{parsed.netloc}/{"/".join(segments[:-1])}' if user else parsed.netloc

    @property
    def cache_key(self) -> str:
        return self._cache_key

    @property
    def label(self) -> str:
        return self._label

    def request_url(self, method: str) -> str:
        method = method.strip().lstrip('/')
        if not method:
            raise ValueError('Bitrix24 method name is empty.')
        if not method.endswith('.json'):
            method = f'{method}.json'
        return f'{self.base_url}{method}'

    def auth_params(self) -> dict[str, Any]:
        return {}

    async def on_auth_error(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f'WebhookIdentity(label={self._label!r}, cache_key={self._cache_key!r})'

    def __str__(self) -> str:
        return self._label


def coerce_identity(identity: BitrixIdentity | str) -> BitrixIdentity:
    if isinstance(identity, str):
        return WebhookIdentity(identity)
    return identity
