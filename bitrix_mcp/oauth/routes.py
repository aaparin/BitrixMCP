from __future__ import annotations

import asyncio
import html
import time
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse

from bitrix_mcp.oauth.store import AuditEntry, TokenStore

if TYPE_CHECKING:
    from bitrix_mcp.oauth.flow import OAuthFlow
    from bitrix_mcp.oauth.wait import AuthWaitRegistry
    from bitrix_mcp.methods.policy import MethodPolicy


def _page(title: str, body: str, *, ok: bool = True) -> HTMLResponse:
    color = '#0f766e' if ok else '#b91c1c'
    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 0; background: #f8fafc; color: #0f172a; }}
    main {{ max-width: 40rem; margin: 4rem auto; padding: 2rem; background: white; border-radius: 12px;
            box-shadow: 0 10px 30px rgba(15,23,42,.08); }}
    h1 {{ margin-top: 0; color: {color}; font-size: 1.5rem; }}
    p {{ line-height: 1.5; }}
    .muted {{ color: #64748b; font-size: .95rem; }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(title)}</h1>
    {body}
  </main>
</body>
</html>"""
    return HTMLResponse(content)


def register_oauth_routes(
    server: Any,
    *,
    flow: OAuthFlow,
    store: TokenStore,
    wait_registry: AuthWaitRegistry,
    webhook_client_factory: Any,
    policy: MethodPolicy,
) -> None:
    @server.custom_route('/healthz', methods=['GET'])
    async def healthz(_request: Request) -> PlainTextResponse:
        return PlainTextResponse('ok')

    @server.custom_route('/oauth/callback', methods=['GET'])
    async def oauth_callback(request: Request) -> HTMLResponse:
        code = (request.query_params.get('code') or '').strip()
        state = (request.query_params.get('state') or '').strip()
        query_member_id = (request.query_params.get('member_id') or '').strip()

        if not state:
            return _page('Authorization failed', '<p>Missing state parameter.</p>', ok=False)

        auth_state = await asyncio.to_thread(store.consume_auth_state, state)
        if auth_state is None:
            return _page(
                'Authorization failed',
                '<p>This authorization link is invalid, expired, or already used.</p>',
                ok=False,
            )

        if not code:
            return _page('Authorization failed', '<p>Missing authorization code.</p>', ok=False)

        if query_member_id and not await flow.member_id_allowed(query_member_id):
            return _page(
                'Authorization failed',
                '<p>This portal does not match the Bitrix24 portal already linked to this server.</p>',
                ok=False,
            )

        try:
            payload = await flow.exchange_code(code)
        except Exception:
            return _page(
                'Authorization failed',
                '<p>Could not exchange the authorization code. Please try again from chat.</p>',
                ok=False,
            )

        if not payload.member_id or not await flow.member_id_allowed(payload.member_id):
            return _page(
                'Authorization failed',
                '<p>This portal does not match the Bitrix24 portal already linked to this server.</p>',
                ok=False,
            )

        # Verify email via user.current under the new token.
        from bitrix_mcp.bitrix import BitrixClient
        from bitrix_mcp.oauth.identity import OAuthIdentity
        from bitrix_mcp.oauth.resolver import fetch_user_profile, profile_email, profile_display_name

        temp_identity = OAuthIdentity(
            email=auth_state.email,
            member_id=payload.member_id,
            bitrix_user_id=payload.user_id,
            client_endpoint=payload.client_endpoint,
            access_token=payload.access_token,
            expires_at=payload.expires_at,
        )
        client = BitrixClient(temp_identity, policy=policy, http_client=None)
        try:
            profile = await fetch_user_profile(client)
        except Exception:
            await client.aclose()
            return _page(
                'Authorization failed',
                '<p>Could not verify your Bitrix24 profile. Please try again.</p>',
                ok=False,
            )
        finally:
            await client.aclose()

        current_email = profile_email(profile)
        if not current_email or not flow.emails_match(current_email, auth_state.email):
            await asyncio.to_thread(
                store.write_audit,
                AuditEntry(
                    ts=int(time.time()),
                    email=auth_state.email,
                    bitrix_user_id=payload.user_id,
                    identity_kind='oauth',
                    method='oauth.callback',
                    access='write',
                    ownership_result='email_mismatch',
                    dry_run=False,
                    outcome='rejected',
                    error='authorized Bitrix user email did not match chat identity',
                ),
            )
            return _page(
                'Authorization failed',
                '<p>The Bitrix24 account that approved access does not match your chat identity.</p>'
                '<p class="muted">Sign in to Bitrix24 with the same account you use in chat, then try again.</p>',
                ok=False,
            )

        try:
            await flow.save_verified_token(email=auth_state.email, payload=payload)
        except RuntimeError:
            return _page(
                'Authorization failed',
                '<p>This portal does not match the Bitrix24 portal already linked to this server.</p>',
                ok=False,
            )
        await wait_registry.signal(state)
        display = html.escape(profile_display_name(profile) or auth_state.email)
        return _page(
            'Connected',
            f'<p>Bitrix24 access for <strong>{display}</strong> is ready.</p>'
            '<p class="muted">You can return to chat. Your pending action will continue automatically.</p>'
            '<script>setTimeout(function(){ window.close(); }, 1200);</script>',
            ok=True,
        )

    @server.custom_route('/bitrix/app', methods=['GET', 'POST'])
    async def bitrix_app_page(request: Request) -> HTMLResponse:
        """Fallback placement page inside Bitrix24 (AUTH_ID / REFRESH_ID)."""
        if request.method == 'GET':
            return _page(
                'BitrixMCP',
                '<p>Open this local application from Bitrix24 to connect your account.</p>'
                '<p class="muted">If you arrived here directly, use the authorization link from chat instead.</p>',
            )

        form = await request.form()
        auth_id = str(form.get('AUTH_ID') or '').strip()
        refresh_id = str(form.get('REFRESH_ID') or '').strip()
        member_id = str(form.get('member_id') or form.get('MEMBER_ID') or '').strip()
        domain = str(form.get('DOMAIN') or form.get('domain') or '').strip()
        auth_expires = str(form.get('AUTH_EXPIRES') or '3600').strip()

        if not auth_id or not refresh_id:
            return _page(
                'Connection failed',
                '<p>Bitrix24 did not provide placement tokens. Open the app from the portal menu.</p>',
                ok=False,
            )

        pinned = await asyncio.to_thread(store.get_pinned_member_id)
        resolved_member_id = member_id or (pinned or '')
        if not resolved_member_id:
            return _page(
                'Connection failed',
                '<p>Bitrix24 did not provide member_id. Open the app from the portal menu.</p>',
                ok=False,
            )
        if not await flow.member_id_allowed(resolved_member_id):
            return _page(
                'Connection failed',
                '<p>This portal does not match the Bitrix24 portal already linked to this server.</p>',
                ok=False,
            )

        from bitrix_mcp.bitrix import BitrixClient
        from bitrix_mcp.oauth.identity import OAuthIdentity
        from bitrix_mcp.oauth.resolver import fetch_user_profile, profile_email, profile_display_name

        try:
            expires_in = int(auth_expires)
        except ValueError:
            expires_in = 3600
        expires_at = int(time.time()) + max(expires_in, 60)
        client_endpoint = (
            f'https://{domain}/rest/'
            if domain and not domain.startswith('http')
            else (domain.rstrip('/') + '/rest/' if domain else '')
        )
        if not client_endpoint:
            return _page('Connection failed', '<p>Missing portal domain in placement data.</p>', ok=False)

        temp_identity = OAuthIdentity(
            email='placement@unknown',
            member_id=resolved_member_id,
            bitrix_user_id=0,
            client_endpoint=client_endpoint,
            access_token=auth_id,
            expires_at=expires_at,
        )
        client = BitrixClient(temp_identity, policy=policy)
        try:
            profile = await fetch_user_profile(client)
            email = profile_email(profile)
            user_id = int(profile.get('ID') or profile.get('id') or 0)
            if not email or not user_id:
                return _page(
                    'Connection failed',
                    '<p>Could not determine your Bitrix24 user profile.</p>',
                    ok=False,
                )
            if not await flow.accept_member_id(resolved_member_id):
                return _page(
                    'Connection failed',
                    '<p>This portal does not match the Bitrix24 portal already linked to this server.</p>',
                    ok=False,
                )
            await asyncio.to_thread(
                store.save_token,
                member_id=resolved_member_id,
                bitrix_user_id=user_id,
                email=email,
                access_token=auth_id,
                refresh_token=refresh_id,
                expires_at=expires_at,
                client_endpoint=client_endpoint,
                scope='',
                status='active',
            )
            display = html.escape(profile_display_name(profile) or email)
            return _page(
                'Connected',
                f'<p>Signed in as <strong>{display}</strong> ({html.escape(email)}).</p>'
                '<p class="muted">Token status is active. You can return to chat and use write tools.</p>',
            )
        except Exception:
            return _page(
                'Connection failed',
                '<p>Could not store placement tokens. Please try again or use the chat authorization link.</p>',
                ok=False,
            )
        finally:
            await client.aclose()
