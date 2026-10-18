from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from bitrix_mcp.bitrix import BitrixClient
from bitrix_mcp.oauth.resolver import IdentityResolver, ResolvedIdentity, current_access_token
from bitrix_mcp.oauth.store import AuditEntry, TokenStore


class OAuthRuntime:
    def __init__(
        self,
        *,
        settings,
        store: TokenStore | None,
        resolver: IdentityResolver,
        policy,
        ownership_guard,
        webhook_client: BitrixClient | None,
        http_client: httpx.AsyncClient,
    ):
        self.settings = settings
        self.store = store
        self.resolver = resolver
        self.policy = policy
        self.ownership_guard = ownership_guard
        self.webhook_client = webhook_client
        self.http_client = http_client

    def client_for(self, identity) -> BitrixClient:
        # Reuse the server's shared HTTP client.
        return BitrixClient(
            identity,
            timeout=self.settings.request_timeout_seconds,
            policy=self.policy,
            ownership_guard=self.ownership_guard,
            http_client=self.http_client,
        )

    def resolve_from_request(self) -> ResolvedIdentity:
        return self.resolver.resolve(current_access_token())

    def client_for_request(self) -> BitrixClient:
        resolved = self.resolve_from_request()
        if resolved.mode == 'oauth':
            return self.client_for(resolved.identity)
        assert self.webhook_client is not None
        return self.webhook_client

    def ensure_write_identity(self, *, method: str, params: dict[str, Any]) -> ResolvedIdentity | dict[str, Any]:
        """Return ResolvedIdentity, or an error payload when the policy forbids the call."""
        resolved = self.resolve_from_request()
        decision = self.policy.decide(method, params, identity=resolved.identity)
        if decision.allowed:
            return resolved
        if decision.requires_authorization:
            return {
                'error': 'authorization_required',
                'message': (
                    'This action must run as a Bitrix24 user. Reconnect the Bitrix24 connector '
                    'in your MCP client to sign in again.'
                ),
                'method': method,
            }
        return {
            'error': 'forbidden',
            'message': decision.reason,
            'method': method,
            'access': decision.access,
        }

    async def audit(
        self,
        *,
        email: str,
        bitrix_user_id: int | None,
        identity_kind: str,
        method: str,
        access: str,
        ownership_result: str,
        dry_run: bool,
        outcome: str,
        error: str = '',
    ) -> None:
        if self.store is None:
            return
        await asyncio.to_thread(
            self.store.write_audit,
            AuditEntry(
                ts=int(time.time()),
                email=email or '',
                bitrix_user_id=bitrix_user_id,
                identity_kind=identity_kind,
                method=method,
                access=access,
                ownership_result=ownership_result,
                dry_run=dry_run,
                outcome=outcome,
                error=error,
            ),
        )
