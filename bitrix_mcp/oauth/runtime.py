from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from fastmcp import Context

from bitrix_mcp.bitrix import BitrixClient
from bitrix_mcp.oauth.flow import OAuthFlow
from bitrix_mcp.oauth.resolver import IdentityResolver, ResolvedIdentity, extract_request_email
from bitrix_mcp.oauth.store import AuditEntry, TokenStore
from bitrix_mcp.oauth.wait import AuthWaitRegistry


class OAuthRuntime:
    def __init__(
        self,
        *,
        settings,
        store: TokenStore | None,
        flow: OAuthFlow | None,
        resolver: IdentityResolver,
        wait_registry: AuthWaitRegistry | None,
        policy,
        ownership_guard,
        webhook_client: BitrixClient,
    ):
        self.settings = settings
        self.store = store
        self.flow = flow
        self.resolver = resolver
        self.wait_registry = wait_registry
        self.policy = policy
        self.ownership_guard = ownership_guard
        self.webhook_client = webhook_client

    def client_for(self, identity) -> BitrixClient:
        # Reuse the shared HTTP client; ownership of close stays with webhook_client.
        return BitrixClient(
            identity,
            timeout=self.settings.request_timeout_seconds,
            policy=self.policy,
            ownership_guard=self.ownership_guard,
            http_client=self.webhook_client._get_http_client(),  # noqa: SLF001
        )

    async def resolve_from_request(self) -> ResolvedIdentity:
        email = extract_request_email(self.settings.user_email_header)
        return await self.resolver.resolve(email)

    async def ensure_write_identity(
        self,
        *,
        method: str,
        params: dict[str, Any],
        ctx: Context | None = None,
    ) -> ResolvedIdentity | dict[str, Any]:
        """Return ResolvedIdentity or an authorization_required / error payload."""
        resolved = await self.resolve_from_request()
        decision = self.policy.decide(method, params, identity=resolved.identity)
        if decision.allowed:
            return resolved
        if not decision.requires_authorization:
            return {
                'error': 'forbidden',
                'message': decision.reason,
                'method': method,
                'access': decision.access,
            }
        if not resolved.email:
            return {
                'error': 'identity_missing',
                'message': (
                    f'Missing {self.settings.user_email_header} header. '
                    'Write actions require an authenticated user email.'
                ),
            }
        if not self.flow or not self.wait_registry or not self.settings.oauth_enabled:
            return {
                'error': 'authorization_required',
                'message': 'User authorization is required for write actions, but OAuth is disabled.',
            }

        state, authorize_url = await self.flow.begin_authorization(resolved.email)
        event = await self.wait_registry.register(state)
        if ctx is not None:
            await ctx.report_progress(
                5,
                100,
                f'To continue as yourself, open: {authorize_url}',
            )

        async def _progress() -> None:
            if ctx is None:
                return
            elapsed = 0
            while elapsed < self.settings.auth_wait_seconds and not event.is_set():
                await asyncio.sleep(5)
                elapsed += 5
                await ctx.report_progress(
                    min(90, 10 + elapsed),
                    100,
                    'Waiting for Bitrix24 authorization…',
                )

        progress_task = asyncio.create_task(_progress())
        try:
            await asyncio.wait_for(event.wait(), timeout=self.settings.auth_wait_seconds)
        except TimeoutError:
            await self.wait_registry.discard(state)
            return self.flow.authorization_required_payload(authorize_url=authorize_url)
        finally:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task

        resolved = await self.resolver.resolve(resolved.email)
        if resolved.mode != 'oauth':
            return self.flow.authorization_required_payload(
                authorize_url=authorize_url,
                message='Authorization did not complete. Open the link and retry the request.',
            )
        return resolved

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
