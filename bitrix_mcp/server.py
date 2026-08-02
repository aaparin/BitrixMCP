from __future__ import annotations

import asyncio
import secrets
import warnings
from contextlib import asynccontextmanager
from typing import Any, Literal

import logfire
from fastmcp import Context, FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier

from bitrix_mcp.bitrix import BitrixClient
from bitrix_mcp.config import Settings
from bitrix_mcp.crm_metadata import CRM_METADATA_CACHE
from bitrix_mcp.crm_statuses import CRM_STATUSES_CACHE, crm_statuses_list_data, load_crm_statuses
from bitrix_mcp.direct_tools import describe_crm_entity_fields, parse_object_input, parse_string_list_input
from bitrix_mcp.discovery_tools import describe_method_data, list_scopes_data, search_methods_data
from bitrix_mcp.methods.availability import AvailabilityProvider, PORTAL_METHODS_CACHE
from bitrix_mcp.methods.catalog import default_catalog
from bitrix_mcp.methods.policy import MethodPolicy
from bitrix_mcp.people_tools import employees_search_data, tasks_list_for_employee_data
from bitrix_mcp.read_tools import (
    activities_list_data,
    activity_get_data,
    crm_count_data,
    crm_item_get_data,
    crm_items_list_data,
    crm_types_list_data,
    employee_get_data,
    employees_list_data,
    task_get_data,
    tasks_count_data,
    tasks_list_data,
    telephony_calls_list_data,
    telephony_lines_list_data,
)


class StaticBearerTokenVerifier(TokenVerifier):
    def __init__(self, token: str, *, base_url: str | None = None):
        super().__init__(base_url=base_url or None)
        self.token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self.token):
            return None
        return AccessToken(token=token, client_id='static-bearer-token', scopes=[])


def configure_logfire(settings: Settings) -> None:
    if not settings.logfire_enabled:
        return
    try:
        logfire.configure(send_to_logfire='if-token-present')
    except Exception as exc:  # pragma: no cover - tracing must not prevent startup.
        print(f'Logfire is disabled: {exc}')
        return

    if settings.logfire_instrument_httpx:
        try:
            logfire.instrument_httpx()
        except Exception as exc:  # pragma: no cover
            print(f'Logfire httpx instrumentation disabled: {exc}')


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    settings.validate_for_run()

    CRM_METADATA_CACHE.ttl_seconds = settings.crm_metadata_ttl_seconds
    CRM_STATUSES_CACHE.ttl_seconds = settings.crm_statuses_ttl_seconds
    PORTAL_METHODS_CACHE.ttl_seconds = settings.portal_methods_ttl_seconds

    catalog = default_catalog(settings.method_catalog_path or None)
    policy = MethodPolicy(
        catalog=catalog,
        allowed_access=settings.allowed_access,
        allow_unknown_methods=settings.allow_unknown_methods,
        allow_webhook_writes=settings.allow_webhook_writes,
        oauth_enabled=settings.oauth_enabled,
    )
    if settings.allow_unknown_methods:
        warnings.warn(
            'BITRIX_ALLOW_UNKNOWN_METHODS=true: undocumented/unknown methods are treated as read. '
            'This can allow undocumented write-like endpoints.',
            RuntimeWarning,
            stacklevel=2,
        )
    if settings.allow_webhook_writes:
        warnings.warn(
            'BITRIX_ALLOW_WEBHOOK_WRITES=true: webhook identity may perform write/destructive calls.',
            RuntimeWarning,
            stacklevel=2,
        )

    from bitrix_mcp.oauth.flow import OAuthFlow
    from bitrix_mcp.oauth.refresh import RefreshCoordinator
    from bitrix_mcp.oauth.resolver import (
        IdentityResolver,
        extract_request_email,
        fetch_user_profile,
        profile_display_name,
    )
    from bitrix_mcp.oauth.routes import register_oauth_routes
    from bitrix_mcp.oauth.runtime import OAuthRuntime
    from bitrix_mcp.oauth.store import TokenStore
    from bitrix_mcp.oauth.wait import AuthWaitRegistry
    from bitrix_mcp.ownership import OwnershipGuard, default_ownership_rules
    from bitrix_mcp.write_tools import (
        activity_add_data,
        crm_item_add_data,
        crm_item_update_data,
        task_add_data,
        task_update_data,
    )

    store = None
    flow = None
    wait_registry = None
    refresh = None
    if settings.oauth_enabled:
        store = TokenStore(settings.token_db_path, settings.token_encryption_key)
        wait_registry = AuthWaitRegistry()
        flow = OAuthFlow(
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            portal_url=settings.portal_url,
            store=store,
            wait_registry=wait_registry,
            auth_state_ttl_seconds=settings.auth_state_ttl_seconds,
        )
        refresh = RefreshCoordinator(store, flow, skew_seconds=settings.token_refresh_skew_seconds)
        admin_count = len(settings.ownership_admin_emails)
        admin_domains = sorted({email.split('@')[-1] for email in settings.ownership_admin_emails if '@' in email})
        print(
            f'OAuth enabled: ownership admins={admin_count}'
            + (f' domains={admin_domains}' if admin_domains else '')
        )

    ownership_guard = OwnershipGuard(
        rules=default_ownership_rules(task_owner_fields=settings.task_ownership_fields),
        admin_emails=settings.ownership_admin_emails,
        enabled=settings.ownership_enabled,
    )

    bitrix = BitrixClient(
        settings.bitrix_webhook_url,
        timeout=settings.request_timeout_seconds,
        policy=policy,
        ownership_guard=ownership_guard,
    )
    resolver = IdentityResolver(
        webhook_url=settings.bitrix_webhook_url,
        store=store,
        refresh=refresh,
        oauth_enabled=settings.oauth_enabled,
    )
    runtime = OAuthRuntime(
        settings=settings,
        store=store,
        flow=flow,
        resolver=resolver,
        wait_registry=wait_registry,
        policy=policy,
        ownership_guard=ownership_guard,
        webhook_client=bitrix,
    )
    availability = AvailabilityProvider(bitrix, cache=PORTAL_METHODS_CACHE)

    auth = (
        StaticBearerTokenVerifier(settings.bearer_token, base_url=settings.public_base_url or None)
        if settings.bearer_token
        else None
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield
        finally:
            await bitrix.aclose()

    server = FastMCP(
        name='Bitrix24 MCP',
        instructions=(
            'Deterministic Bitrix24 REST MCP (no internal LLM). '
            'Prefer curated tools for CRM, tasks, activities, employees, and telephony. '
            'Read CRM status dictionaries from resource bitrix://crm/statuses '
            '(or bitrix://crm/statuses/{entity_id}). '
            'Writes require per-user OAuth: use bitrix_whoami / bitrix_authorize. '
            'Write tools default to dry_run=true; set dry_run=false to commit. '
            'OwnershipGuard allows updates only for records you are responsible for '
            '(unless configured as an ownership admin). '
            'Escape hatch: bitrix_call(method, params) under the same policy.'
        ),
        auth=auth,
        lifespan=lifespan,
    )

    if settings.oauth_enabled and flow is not None and store is not None and wait_registry is not None:
        register_oauth_routes(
            server,
            flow=flow,
            store=store,
            wait_registry=wait_registry,
            webhook_client_factory=lambda: bitrix,
            policy=policy,
        )
    else:
        @server.custom_route('/healthz', methods=['GET'])
        async def healthz(_request):  # type: ignore[no-untyped-def]
            from starlette.responses import PlainTextResponse

            return PlainTextResponse('ok')

    async def bitrix_client(ctx: Context | None = None) -> BitrixClient:
        resolved = await runtime.resolve_from_request()
        if resolved.mode == 'oauth':
            return runtime.client_for(resolved.identity)
        return bitrix

    @server.resource(
        'bitrix://crm/statuses',
        mime_type='application/json',
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def crm_statuses_resource() -> dict[str, Any]:
        """Full CRM status dictionary index grouped by ENTITY_ID."""
        return await load_crm_statuses(await bitrix_client())

    @server.resource(
        'bitrix://crm/statuses/{entity_id}',
        mime_type='application/json',
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def crm_statuses_entity_resource(entity_id: str) -> dict[str, Any]:
        """CRM status dictionary for one ENTITY_ID (STATUS, DEAL_STAGE, SOURCE, ...)."""
        return await load_crm_statuses(await bitrix_client(), entity_id)

    @server.tool
    async def crm_statuses_list(
        entity_id: str | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """List CRM status dictionary entries (crm.status.list), optionally for one ENTITY_ID.

        Args:
            entity_id: Dictionary group such as STATUS (leads), DEAL_STAGE, SOURCE, COMPANY_TYPE.
                Omit to return the full index grouped by entityId.
            force_refresh: Bypass the short-lived statuses cache and reload from Bitrix24.
        """
        return await crm_statuses_list_data(
            await bitrix_client(),
            entity_id,
            force_refresh=force_refresh,
        )

    @server.tool
    async def bitrix_search_methods(
        query: str = '',
        scope: str | None = None,
        access: Literal['read', 'write', 'destructive', 'unknown'] | None = None,
        includeDeprecated: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search the Bitrix24 REST method catalog by name fragments, scope, and access level."""
        return await search_methods_data(
            catalog,
            query=query,
            scope=scope,
            access=access,
            include_deprecated=includeDeprecated,
            limit=limit,
            availability=availability,
        )

    @server.tool
    async def bitrix_describe_method(method: str) -> dict[str, Any]:
        """Describe one Bitrix24 REST method, including access policy and portal availability."""
        return await describe_method_data(
            catalog,
            method,
            policy=policy,
            availability=availability,
        )

    @server.tool
    async def bitrix_list_scopes() -> dict[str, Any]:
        """List catalog scopes and summarize which methods/scopes the current webhook can use."""
        return await list_scopes_data(catalog, availability=availability)

    @server.tool
    async def crm_types_list(
        filter: dict[str, Any] | str | None = None,
        order: dict[str, Any] | str | None = None,
        start: int = 0,
    ) -> dict[str, Any]:
        """List smart-process CRM types and their entityTypeId values."""
        return await crm_types_list_data(
            await bitrix_client(),
            filter=filter,
            order=order,
            start=start,
        )

    @server.tool
    async def crm_items_list(
        entity: str | int,
        filter: dict[str, Any] | str | None = None,
        select: list[str] | str | None = None,
        order: dict[str, Any] | str | None = None,
        start: int = 0,
        use_original_userfield_names: bool = True,
    ) -> dict[str, Any]:
        """List CRM items for leads, deals, contacts, companies, quotes, invoices, or smart processes.

        Args:
            entity: Entity alias, numeric entityTypeId, or DYNAMIC_<entityTypeId>.
            filter: Bitrix24 crm.item.list filter object.
            select: Fields to return. Use ["*"] to include multi-fields such as phones and email.
            order: Sort object such as {"id": "DESC"}.
            start: Non-negative pagination offset returned as next by Bitrix24.
            use_original_userfield_names: Return custom fields as UF_CRM_* names.
        """
        return await crm_items_list_data(
            await bitrix_client(),
            entity,
            filter=filter,
            select=select,
            order=order,
            start=start,
            use_original_userfield_names=use_original_userfield_names,
        )

    @server.tool
    async def crm_item_get(
        entity: str | int,
        item_id: int,
        use_original_userfield_names: bool = True,
    ) -> dict[str, Any]:
        """Get one CRM item by entity alias/entityTypeId and item ID."""
        return await crm_item_get_data(
            await bitrix_client(),
            entity,
            item_id,
            use_original_userfield_names=use_original_userfield_names,
        )

    @server.tool
    async def crm_count(
        entity: Literal['company', 'contact', 'deal', 'lead'],
        filter: dict[str, Any] | str | None = None,
        conditions: list[dict[str, Any]] | str | None = None,
    ) -> dict[str, Any]:
        """Count CRM companies, contacts, deals, or leads using Bitrix24 total without pagination.

        Args:
            entity: CRM entity to count.
            filter: Bitrix24 filter. Human aliases such as {"status": "won", "year": 2026} are resolved.
            conditions: Optional structured conditions, for example
                [{"field": "status", "value": "won"}, {"field": "close date", "operator": "year", "value": 2026}].
        """
        return await crm_count_data(
            await bitrix_client(),
            entity,
            filter=filter,
            conditions=conditions,
        )

    @server.tool
    async def crm_describe_fields(
        entities: list[str] | str,
        fieldNames: list[str] | str | None = None,
        includeEnums: bool = True,
        includeSystemFields: bool = True,
    ) -> dict[str, Any]:
        """Describe CRM fields and user fields for static entities and smart processes.

        Args:
            entities: Entity names such as deal, contact, or DYNAMIC_<entityTypeId>.
            fieldNames: Optional FIELD_NAME filter. When set, only matching fields/user fields are returned.
            includeEnums: Include enum/list values when available.
            includeSystemFields: When false, return only user fields (replaces crm_userfields_export).
        """
        entity_names = parse_string_list_input(entities, name='entities')
        field_names = parse_string_list_input(fieldNames, name='fieldNames') if fieldNames is not None else []
        client = await bitrix_client()
        described: dict[str, Any] = {}
        for entity in entity_names:
            described[entity] = await describe_crm_entity_fields(
                client,
                entity,
                field_names or None,
                include_enums=includeEnums,
                include_system_fields=includeSystemFields,
            )
        return {'entities': described}

    @server.tool
    async def tasks_list(
        filter: dict[str, Any] | str | None = None,
        select: list[str] | str | None = None,
        order: dict[str, Any] | str | None = None,
        start: int = 0,
    ) -> dict[str, Any]:
        """List tasks with filters, selected fields, sorting, pagination, and total."""
        return await tasks_list_data(
            await bitrix_client(),
            filter=filter,
            select=select,
            order=order,
            start=start,
        )

    @server.tool
    async def task_get(
        task_id: int,
        select: list[str] | str | None = None,
    ) -> dict[str, Any]:
        """Get one task by numeric ID."""
        return await task_get_data(await bitrix_client(), task_id, select=select)

    @server.tool
    async def tasks_count(filter: dict[str, Any] | str | None = None) -> dict[str, Any]:
        """Count Bitrix24 tasks using total without pagination."""
        return await tasks_count_data(await bitrix_client(), filter=filter)

    @server.tool
    async def tasks_list_for_employee(
        user_name: str,
        status: str | int | None = 'open',
        limit: int = 10,
    ) -> dict[str, Any]:
        """List tasks where a named Bitrix24 employee/user is responsible."""
        return await tasks_list_for_employee_data(
            await bitrix_client(),
            user_name,
            status=status,
            limit=limit,
        )

    @server.tool
    async def activities_list(
        filter: dict[str, Any] | str | None = None,
        select: list[str] | str | None = None,
        order: dict[str, Any] | str | None = None,
        start: int = 0,
    ) -> dict[str, Any]:
        """List CRM activities such as calls, emails, meetings, and to-dos."""
        return await activities_list_data(
            await bitrix_client(),
            filter=filter,
            select=select,
            order=order,
            start=start,
        )

    @server.tool
    async def activity_get(activity_id: int) -> dict[str, Any]:
        """Get one CRM activity by numeric ID."""
        return await activity_get_data(await bitrix_client(), activity_id)

    @server.tool
    async def employees_list(
        filter: dict[str, Any] | str | None = None,
        select: list[str] | str | None = None,
        sort: str = 'ID',
        order: str = 'ASC',
        start: int = 0,
    ) -> dict[str, Any]:
        """List Bitrix24 employees/users with filtering, sorting, and pagination."""
        return await employees_list_data(
            await bitrix_client(),
            filter=filter,
            select=select,
            sort=sort,
            order=order,
            start=start,
        )

    @server.tool
    async def employee_get(employee_id: int) -> dict[str, Any]:
        """Get one Bitrix24 employee/user by numeric ID."""
        return await employee_get_data(await bitrix_client(), employee_id)

    @server.tool
    async def employees_search(query: str, limit: int = 10) -> dict[str, Any]:
        """Search Bitrix24 employees/users by name or email."""
        return await employees_search_data(await bitrix_client(), query, limit=limit)

    @server.tool
    async def telephony_calls_list(
        filter: dict[str, Any] | str | None = None,
        sort: str = 'CALL_START_DATE',
        order: str = 'DESC',
        start: int = 0,
    ) -> dict[str, Any]:
        """List telephony call statistics with filtering, sorting, pagination, and total."""
        return await telephony_calls_list_data(
            await bitrix_client(),
            filter=filter,
            sort=sort,
            order=order,
            start=start,
        )

    @server.tool
    async def telephony_lines_list() -> dict[str, Any]:
        """List available outgoing telephony lines without exposing SIP credentials."""
        return await telephony_lines_list_data(await bitrix_client())

    @server.tool
    async def bitrix_call(
        method: str,
        params: dict[str, Any] | str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Call a Bitrix24 REST method directly. Access is enforced by MethodPolicy (default: read only)."""
        parsed_params = parse_object_input(params, name='params')
        ensured = await runtime.ensure_write_identity(method=method, params=parsed_params, ctx=ctx)
        if isinstance(ensured, dict):
            return ensured
        client = runtime.client_for(ensured.identity) if ensured.mode == 'oauth' else bitrix
        decision = policy.decide(method, parsed_params, identity=ensured.identity)
        if decision.access in {'write', 'destructive'}:
            await runtime.audit(
                email=ensured.email or '',
                bitrix_user_id=ensured.bitrix_user_id,
                identity_kind=ensured.mode,
                method=method,
                access=decision.access,
                ownership_result='pending',
                dry_run=False,
                outcome='intent',
            )
        try:
            payload = await client.call_method_payload(method, parsed_params)
        except Exception as exc:
            if decision.access in {'write', 'destructive'}:
                await runtime.audit(
                    email=ensured.email or '',
                    bitrix_user_id=ensured.bitrix_user_id,
                    identity_kind=ensured.mode,
                    method=method,
                    access=decision.access,
                    ownership_result='error',
                    dry_run=False,
                    outcome='error',
                    error=str(exc),
                )
            raise
        if decision.access in {'write', 'destructive'}:
            await runtime.audit(
                email=ensured.email or '',
                bitrix_user_id=ensured.bitrix_user_id,
                identity_kind=ensured.mode,
                method=method,
                access=decision.access,
                ownership_result='ok',
                dry_run=False,
                outcome='ok',
            )
        return payload if isinstance(payload, dict) else {'result': payload}

    async def _write_client(method: str, params: dict[str, Any], ctx: Context | None):
        ensured = await runtime.ensure_write_identity(method=method, params=params, ctx=ctx)
        if isinstance(ensured, dict):
            return ensured, None
        return ensured, runtime.client_for(ensured.identity)

    @server.tool
    async def bitrix_whoami(ctx: Context | None = None) -> dict[str, Any]:
        """Show the current Bitrix identity: email, user id, auth status, and token expiry."""
        _ = ctx
        resolved = await runtime.resolve_from_request()
        display_name = resolved.display_name
        if resolved.mode == 'oauth' and not display_name:
            try:
                profile = await fetch_user_profile(runtime.client_for(resolved.identity))
                display_name = profile_display_name(profile)
            except Exception:
                display_name = None
        return {
            'email': resolved.email,
            'mode': resolved.mode,
            'bitrixUserId': resolved.bitrix_user_id,
            'displayName': display_name,
            'authorized': resolved.mode == 'oauth',
            'tokenExpiresAt': resolved.token_expires_at,
            'oauthEnabled': settings.oauth_enabled,
        }

    @server.tool
    async def bitrix_authorize(ctx: Context | None = None) -> dict[str, Any]:
        """Return a Bitrix24 authorization link for the current chat user (idempotent)."""
        _ = ctx
        if not settings.oauth_enabled or flow is None:
            return {
                'error': 'oauth_disabled',
                'message': 'Per-user OAuth is disabled on this server.',
            }
        email = extract_request_email(settings.user_email_header)
        if not email:
            return {
                'error': 'identity_missing',
                'message': (
                    f'Missing {settings.user_email_header} header. '
                    'Authorization requires an authenticated user email.'
                ),
            }
        resolved = await resolver.resolve(email)
        if resolved.mode == 'oauth':
            return {
                'authorized': True,
                'email': resolved.email,
                'bitrixUserId': resolved.bitrix_user_id,
                'tokenExpiresAt': resolved.token_expires_at,
                'message': 'Already authorized. Write tools can run as this user.',
            }
        state, authorize_url = await flow.begin_authorization(email)
        return {
            'authorized': False,
            'email': email,
            'authorizeUrl': authorize_url,
            'state': state,
            'expiresInSeconds': settings.auth_state_ttl_seconds,
            'message': 'Open the authorizeUrl in a browser to connect your Bitrix24 account.',
        }

    @server.tool
    async def bitrix_revoke(ctx: Context | None = None) -> dict[str, Any]:
        """Revoke the stored OAuth token for the current chat user."""
        _ = ctx
        if not settings.oauth_enabled or store is None:
            return {
                'error': 'oauth_disabled',
                'message': 'Per-user OAuth is disabled on this server.',
            }
        email = extract_request_email(settings.user_email_header)
        if not email:
            return {
                'error': 'identity_missing',
                'message': f'Missing {settings.user_email_header} header.',
            }
        token = await asyncio.to_thread(store.get_by_email, email)
        if token is None:
            return {'revoked': False, 'email': email, 'message': 'No active token found for this user.'}
        await asyncio.to_thread(store.mark_revoked, token.member_id, token.bitrix_user_id)
        return {
            'revoked': True,
            'email': email,
            'bitrixUserId': token.bitrix_user_id,
            'message': 'Token revoked. Call bitrix_authorize to reconnect.',
        }

    @server.tool
    async def crm_item_add(
        entity: str | int,
        fields: dict[str, Any] | str,
        dry_run: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a CRM item (deal/lead/…). Defaults to dry_run=true."""
        from bitrix_mcp.read_tools import resolve_crm_entity_type_id

        entity_type_id = resolve_crm_entity_type_id(entity)
        parsed_fields = parse_object_input(fields, name='fields')
        params = {'entityTypeId': entity_type_id, 'fields': parsed_fields}
        ensured, client = await _write_client('crm.item.add', params, ctx)
        if client is None:
            return ensured  # type: ignore[return-value]
        await runtime.audit(
            email=ensured.email or '',
            bitrix_user_id=ensured.bitrix_user_id,
            identity_kind=ensured.mode,
            method='crm.item.add',
            access='write',
            ownership_result='pending',
            dry_run=dry_run,
            outcome='intent',
        )
        try:
            result = await crm_item_add_data(client, entity, fields, dry_run=dry_run)
        except Exception as exc:
            await runtime.audit(
                email=ensured.email or '',
                bitrix_user_id=ensured.bitrix_user_id,
                identity_kind=ensured.mode,
                method='crm.item.add',
                access='write',
                ownership_result='error',
                dry_run=dry_run,
                outcome='error',
                error=str(exc),
            )
            raise
        ownership_result = 'ok'
        if isinstance(result, dict) and isinstance(result.get('ownership'), dict):
            ownership_result = str(result['ownership'].get('result') or 'ok')
        await runtime.audit(
            email=ensured.email or '',
            bitrix_user_id=ensured.bitrix_user_id,
            identity_kind=ensured.mode,
            method='crm.item.add',
            access='write',
            ownership_result=ownership_result,
            dry_run=dry_run,
            outcome='ok' if result.get('error') != 'ownership_denied' else 'rejected',
        )
        return result

    @server.tool
    async def crm_item_update(
        entity: str | int,
        item_id: int,
        fields: dict[str, Any] | str,
        dry_run: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Update a CRM item you are responsible for. Defaults to dry_run=true."""
        from bitrix_mcp.read_tools import resolve_crm_entity_type_id

        entity_type_id = resolve_crm_entity_type_id(entity)
        parsed_fields = parse_object_input(fields, name='fields')
        params = {'entityTypeId': entity_type_id, 'id': item_id, 'fields': parsed_fields}
        ensured, client = await _write_client('crm.item.update', params, ctx)
        if client is None:
            return ensured  # type: ignore[return-value]
        await runtime.audit(
            email=ensured.email or '',
            bitrix_user_id=ensured.bitrix_user_id,
            identity_kind=ensured.mode,
            method='crm.item.update',
            access='write',
            ownership_result='pending',
            dry_run=dry_run,
            outcome='intent',
        )
        try:
            result = await crm_item_update_data(client, entity, item_id, fields, dry_run=dry_run)
        except Exception as exc:
            await runtime.audit(
                email=ensured.email or '',
                bitrix_user_id=ensured.bitrix_user_id,
                identity_kind=ensured.mode,
                method='crm.item.update',
                access='write',
                ownership_result='error',
                dry_run=dry_run,
                outcome='error',
                error=str(exc),
            )
            raise
        ownership_result = 'ok'
        if isinstance(result, dict) and isinstance(result.get('ownership'), dict):
            ownership_result = str(result['ownership'].get('result') or 'ok')
        await runtime.audit(
            email=ensured.email or '',
            bitrix_user_id=ensured.bitrix_user_id,
            identity_kind=ensured.mode,
            method='crm.item.update',
            access='write',
            ownership_result=ownership_result,
            dry_run=dry_run,
            outcome='ok' if result.get('error') != 'ownership_denied' else 'rejected',
        )
        return result

    @server.tool
    async def task_add(
        fields: dict[str, Any] | str,
        dry_run: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a task. Defaults to dry_run=true; responsible is forced to the current user."""
        parsed_fields = parse_object_input(fields, name='fields')
        params = {'fields': parsed_fields}
        ensured, client = await _write_client('tasks.task.add', params, ctx)
        if client is None:
            return ensured  # type: ignore[return-value]
        await runtime.audit(
            email=ensured.email or '',
            bitrix_user_id=ensured.bitrix_user_id,
            identity_kind=ensured.mode,
            method='tasks.task.add',
            access='write',
            ownership_result='pending',
            dry_run=dry_run,
            outcome='intent',
        )
        try:
            result = await task_add_data(client, fields, dry_run=dry_run)
        except Exception as exc:
            await runtime.audit(
                email=ensured.email or '',
                bitrix_user_id=ensured.bitrix_user_id,
                identity_kind=ensured.mode,
                method='tasks.task.add',
                access='write',
                ownership_result='error',
                dry_run=dry_run,
                outcome='error',
                error=str(exc),
            )
            raise
        ownership_result = 'ok'
        if isinstance(result, dict) and isinstance(result.get('ownership'), dict):
            ownership_result = str(result['ownership'].get('result') or 'ok')
        await runtime.audit(
            email=ensured.email or '',
            bitrix_user_id=ensured.bitrix_user_id,
            identity_kind=ensured.mode,
            method='tasks.task.add',
            access='write',
            ownership_result=ownership_result,
            dry_run=dry_run,
            outcome='ok' if result.get('error') != 'ownership_denied' else 'rejected',
        )
        return result

    @server.tool
    async def task_update(
        task_id: int,
        fields: dict[str, Any] | str,
        dry_run: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Update a task you own. Defaults to dry_run=true."""
        parsed_fields = parse_object_input(fields, name='fields')
        params = {'taskId': task_id, 'fields': parsed_fields}
        ensured, client = await _write_client('tasks.task.update', params, ctx)
        if client is None:
            return ensured  # type: ignore[return-value]
        await runtime.audit(
            email=ensured.email or '',
            bitrix_user_id=ensured.bitrix_user_id,
            identity_kind=ensured.mode,
            method='tasks.task.update',
            access='write',
            ownership_result='pending',
            dry_run=dry_run,
            outcome='intent',
        )
        try:
            result = await task_update_data(client, task_id, fields, dry_run=dry_run)
        except Exception as exc:
            await runtime.audit(
                email=ensured.email or '',
                bitrix_user_id=ensured.bitrix_user_id,
                identity_kind=ensured.mode,
                method='tasks.task.update',
                access='write',
                ownership_result='error',
                dry_run=dry_run,
                outcome='error',
                error=str(exc),
            )
            raise
        ownership_result = 'ok'
        if isinstance(result, dict) and isinstance(result.get('ownership'), dict):
            ownership_result = str(result['ownership'].get('result') or 'ok')
        await runtime.audit(
            email=ensured.email or '',
            bitrix_user_id=ensured.bitrix_user_id,
            identity_kind=ensured.mode,
            method='tasks.task.update',
            access='write',
            ownership_result=ownership_result,
            dry_run=dry_run,
            outcome='ok' if result.get('error') != 'ownership_denied' else 'rejected',
        )
        return result

    @server.tool
    async def activity_add(
        fields: dict[str, Any] | str,
        dry_run: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a CRM activity. Defaults to dry_run=true."""
        parsed_fields = parse_object_input(fields, name='fields')
        params = {'fields': parsed_fields}
        ensured, client = await _write_client('crm.activity.add', params, ctx)
        if client is None:
            return ensured  # type: ignore[return-value]
        await runtime.audit(
            email=ensured.email or '',
            bitrix_user_id=ensured.bitrix_user_id,
            identity_kind=ensured.mode,
            method='crm.activity.add',
            access='write',
            ownership_result='pending',
            dry_run=dry_run,
            outcome='intent',
        )
        try:
            result = await activity_add_data(client, fields, dry_run=dry_run)
        except Exception as exc:
            await runtime.audit(
                email=ensured.email or '',
                bitrix_user_id=ensured.bitrix_user_id,
                identity_kind=ensured.mode,
                method='crm.activity.add',
                access='write',
                ownership_result='error',
                dry_run=dry_run,
                outcome='error',
                error=str(exc),
            )
            raise
        ownership_result = 'ok'
        if isinstance(result, dict) and isinstance(result.get('ownership'), dict):
            ownership_result = str(result['ownership'].get('result') or 'ok')
        await runtime.audit(
            email=ensured.email or '',
            bitrix_user_id=ensured.bitrix_user_id,
            identity_kind=ensured.mode,
            method='crm.activity.add',
            access='write',
            ownership_result=ownership_result,
            dry_run=dry_run,
            outcome='ok' if result.get('error') != 'ownership_denied' else 'rejected',
        )
        return result

    return server


def main() -> None:
    settings = Settings.from_env()
    settings.validate_for_run()
    configure_logfire(settings)
    server = create_server(settings)
    server.run(
        transport=settings.transport,
        host=settings.host,
        port=settings.port,
        path=settings.path,
    )


if __name__ == '__main__':
    main()
