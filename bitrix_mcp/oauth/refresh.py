from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from bitrix_mcp.oauth.store import StoredToken

if TYPE_CHECKING:
    from bitrix_mcp.oauth.flow import OAuthFlow
    from bitrix_mcp.oauth.store import TokenStore


class RefreshCoordinator:
    """Single-flight refresh per user to survive refresh_token rotation."""

    def __init__(self, store: TokenStore, flow: OAuthFlow, *, skew_seconds: int = 300):
        self.store = store
        self.flow = flow
        self.skew_seconds = skew_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._meta_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def _key(self, member_id: str, user_id: int) -> str:
        return f'{member_id}|{user_id}'

    def needs_refresh(self, token: StoredToken, *, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        return token.expires_at - now <= self.skew_seconds

    async def ensure_fresh(self, token: StoredToken) -> StoredToken:
        if not self.needs_refresh(token):
            return token
        return await self.refresh(token)

    async def refresh(self, token: StoredToken) -> StoredToken:
        key = self._key(token.member_id, token.bitrix_user_id)
        lock = await self._lock_for(key)
        async with lock:
            latest = await asyncio.to_thread(
                self.store.get_by_user_id,
                token.member_id,
                token.bitrix_user_id,
            )
            if latest is not None and not self.needs_refresh(latest):
                return latest
            current = latest or token
            try:
                payload = await self.flow.refresh(current.refresh_token)
            except Exception:
                await asyncio.to_thread(
                    self.store.mark_revoked,
                    current.member_id,
                    current.bitrix_user_id,
                )
                raise
            return await asyncio.to_thread(
                self.store.save_token,
                member_id=payload.member_id or current.member_id,
                bitrix_user_id=payload.user_id or current.bitrix_user_id,
                email=current.email,
                access_token=payload.access_token,
                refresh_token=payload.refresh_token,
                expires_at=payload.expires_at,
                client_endpoint=payload.client_endpoint or current.client_endpoint,
                scope=payload.scope or current.scope,
                status='active',
            )
