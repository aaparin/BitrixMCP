from __future__ import annotations

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
    )
    if settings.allow_unknown_methods:
        warnings.warn(
            'BITRIX_ALLOW_UNKNOWN_METHODS=true: undocumented/unknown methods are treated as read. '
            'This can allow undocumented write-like endpoints.',
            RuntimeWarning,
            stacklevel=2,
        )

    bitrix = BitrixClient(
        settings.bitrix_webhook_url,
        timeout=settings.request_timeout_seconds,
        policy=policy,
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
            '(or bitrix://crm/statuses/{entity_id} for one group such as DEAL_STAGE / STATUS), '
            'or call crm_statuses_list when you need an explicit filtered fetch. '
            'Discover endpoints with bitrix_search_methods / bitrix_describe_method / bitrix_list_scopes, '
            'then call them via curated tools or bitrix_call(method, params). '
            'crm_describe_fields covers both system fields and user fields '
            '(use fieldNames to filter; includeSystemFields=false for user fields only). '
            'ask_bitrix and list_capabilities were removed — use discovery + curated tools instead. '
            'call_bitrix_rest was renamed to bitrix_call. '
            'Default policy allows read methods only; writes/destructive calls are blocked unless '
            'BITRIX_ALLOWED_ACCESS is expanded.'
        ),
        auth=auth,
        lifespan=lifespan,
    )

    def bitrix_client(ctx: Context | None = None) -> BitrixClient:
        # ctx reserved for future per-identity registries (OAuth).
        _ = ctx
        return bitrix

    @server.resource(
        'bitrix://crm/statuses',
        mime_type='application/json',
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def crm_statuses_resource() -> dict[str, Any]:
        """Full CRM status dictionary index grouped by ENTITY_ID."""
        return await load_crm_statuses(bitrix_client())

    @server.resource(
        'bitrix://crm/statuses/{entity_id}',
        mime_type='application/json',
        annotations={'readOnlyHint': True, 'idempotentHint': True},
    )
    async def crm_statuses_entity_resource(entity_id: str) -> dict[str, Any]:
        """CRM status dictionary for one ENTITY_ID (STATUS, DEAL_STAGE, SOURCE, ...)."""
        return await load_crm_statuses(bitrix_client(), entity_id)

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
            bitrix_client(),
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
            bitrix_client(),
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
            bitrix_client(),
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
            bitrix_client(),
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
            bitrix_client(),
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
        client = bitrix_client()
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
            bitrix_client(),
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
        return await task_get_data(bitrix_client(), task_id, select=select)

    @server.tool
    async def tasks_count(filter: dict[str, Any] | str | None = None) -> dict[str, Any]:
        """Count Bitrix24 tasks using total without pagination."""
        return await tasks_count_data(bitrix_client(), filter=filter)

    @server.tool
    async def tasks_list_for_employee(
        user_name: str,
        status: str | int | None = 'open',
        limit: int = 10,
    ) -> dict[str, Any]:
        """List tasks where a named Bitrix24 employee/user is responsible."""
        return await tasks_list_for_employee_data(
            bitrix_client(),
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
            bitrix_client(),
            filter=filter,
            select=select,
            order=order,
            start=start,
        )

    @server.tool
    async def activity_get(activity_id: int) -> dict[str, Any]:
        """Get one CRM activity by numeric ID."""
        return await activity_get_data(bitrix_client(), activity_id)

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
            bitrix_client(),
            filter=filter,
            select=select,
            sort=sort,
            order=order,
            start=start,
        )

    @server.tool
    async def employee_get(employee_id: int) -> dict[str, Any]:
        """Get one Bitrix24 employee/user by numeric ID."""
        return await employee_get_data(bitrix_client(), employee_id)

    @server.tool
    async def employees_search(query: str, limit: int = 10) -> dict[str, Any]:
        """Search Bitrix24 employees/users by name or email."""
        return await employees_search_data(bitrix_client(), query, limit=limit)

    @server.tool
    async def telephony_calls_list(
        filter: dict[str, Any] | str | None = None,
        sort: str = 'CALL_START_DATE',
        order: str = 'DESC',
        start: int = 0,
    ) -> dict[str, Any]:
        """List telephony call statistics with filtering, sorting, pagination, and total."""
        return await telephony_calls_list_data(
            bitrix_client(),
            filter=filter,
            sort=sort,
            order=order,
            start=start,
        )

    @server.tool
    async def telephony_lines_list() -> dict[str, Any]:
        """List available outgoing telephony lines without exposing SIP credentials."""
        return await telephony_lines_list_data(bitrix_client())

    @server.tool
    async def bitrix_call(method: str, params: dict[str, Any] | str | None = None) -> dict[str, Any]:
        """Call a Bitrix24 REST method directly. Access is enforced by MethodPolicy (default: read only)."""
        parsed_params = parse_object_input(params, name='params')
        payload = await bitrix_client().call_method_payload(method, parsed_params)
        return payload if isinstance(payload, dict) else {'result': payload}

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
