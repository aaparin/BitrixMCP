from __future__ import annotations

import secrets
from typing import Any

import logfire
from fastmcp import Context, FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier

from bitrix_mcp.agent import DOCS_CACHE, answer_question
from bitrix_mcp.bitrix import BitrixClient
from bitrix_mcp.config import Settings
from bitrix_mcp.direct_tools import (
    describe_crm_entity_fields,
    enrich_userfield_from_get,
    parse_object_input,
    parse_string_list_input,
    list_crm_userfields,
    normalize_userfield,
    resolve_crm_entity,
)
from bitrix_mcp.read_tools import (
    activities_list_data,
    activity_get_data,
    crm_item_get_data,
    crm_items_list_data,
    crm_types_list_data,
    employee_get_data,
    employees_list_data,
    task_get_data,
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
        logfire.instrument_pydantic_ai()
        if settings.logfire_instrument_httpx:
            logfire.instrument_httpx()
    except Exception as exc:  # pragma: no cover - tracing must not prevent startup.
        print(f'Logfire is disabled: {exc}')


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    DOCS_CACHE.ttl_seconds = settings.docs_cache_ttl_seconds
    auth = (
        StaticBearerTokenVerifier(settings.bearer_token, base_url=settings.public_base_url or None)
        if settings.bearer_token
        else None
    )

    server = FastMCP(
        name='Bitrix24 AI MCP',
        instructions=(
            'Prefer the deterministic read tools for CRM, tasks, activities, employees, and telephony. '
            'Use ask_bitrix(question) only for natural-language questions that require multi-step reasoning. '
            'Send questions in English when possible. If another language is received, the server translates it '
            'internally and still answers in English. '
            'To force the stronger OpenRouter model for a single request, start the question with "use openrouter:". '
            'The server keeps Bitrix24 credentials private and defaults to read-only REST methods.'
        ),
        auth=auth,
    )

    def bitrix_client() -> BitrixClient:
        if not settings.bitrix_webhook_url:
            raise RuntimeError('Missing required environment variable: BITRIX_WEBHOOK_URL')
        return BitrixClient(
            settings.bitrix_webhook_url,
            timeout=settings.request_timeout_seconds,
            read_only=settings.read_only,
        )

    @server.tool
    async def ask_bitrix(question: str, ctx: Context) -> str:
        """Answer a natural-language question using Bitrix24 REST API data."""
        await ctx.report_progress(0, 100, 'Preparing Bitrix24 agent')
        try:
            settings.validate_for_run()
        except RuntimeError as exc:
            return f'Сервер не настроен: {exc}'

        await ctx.report_progress(10, 100, 'Looking up Bitrix24 REST documentation')
        with logfire.span('ask_bitrix', question=question):
            answer = await answer_question(question, settings)

        await ctx.report_progress(100, 100, 'Done')
        return answer

    @server.tool
    async def list_capabilities() -> str:
        """Describe what this MCP server can answer."""
        mode = 'read-only' if settings.read_only else 'read-write'
        return (
            f'Bitrix24 AI MCP server ({mode}). '
            'It provides deterministic read tools for CRM items and types, tasks, CRM activities, '
            'employees, and telephony calls and lines. It also answers English natural-language questions '
            'by consulting Bitrix24 REST API docs '
            'through the configured documentation MCP server and then calling Bitrix24 REST API. '
            'Non-English questions are translated internally and answered in English. '
            'MVP domains: users, tasks, CRM, calendar, and other read-only REST methods available to the webhook. '
            'The server uses a local OpenAI-compatible LLM first and automatically retries the same request with OpenRouter '
            'when the local model cannot complete the agent workflow. '
            'Prefix a question with "use openrouter:" to skip Ollama for that one request.'
        )

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
    async def call_bitrix_rest(method: str, params: dict[str, Any] | str | None = None) -> dict[str, Any]:
        """Call a read-only Bitrix24 REST method directly without LLM planning."""
        parsed_params = parse_object_input(params, name='params')
        payload = await bitrix_client().call_method_payload(method, parsed_params)
        return payload if isinstance(payload, dict) else {'result': payload}

    @server.tool
    async def crm_userfield_lookup(
        entity: str,
        field_names: list[str] | str,
        include_enums: bool = True,
    ) -> dict[str, Any]:
        """Find CRM user fields by FIELD_NAME and return compact JSON metadata."""
        names = parse_string_list_input(field_names, name='field_names')
        client = bitrix_client()
        resolved = await resolve_crm_entity(client, entity)
        userfields = await list_crm_userfields(client, resolved)
        wanted = {name.upper() for name in names}
        fields: list[dict[str, Any]] = []
        for field in userfields:
            field_name = str(field.get('FIELD_NAME') or field.get('fieldName') or field.get('name') or '')
            if field_name.upper() not in wanted:
                continue
            detailed = field
            if include_enums:
                normalized = normalize_userfield(field, include_enums=True)
                if not normalized.get('enum'):
                    detailed = await enrich_userfield_from_get(client, resolved, field)
            fields.append(normalize_userfield(detailed, include_enums=include_enums))
        return {'entity': entity, 'fieldNames': names, 'fields': fields}

    @server.tool
    async def crm_userfields_export(
        entities: list[str] | str,
        include_enums: bool = True,
    ) -> dict[str, Any]:
        """Export all CRM user fields for selected entities as compact JSON."""
        entity_names = parse_string_list_input(entities, name='entities')
        client = bitrix_client()
        exported: dict[str, Any] = {}
        for entity in entity_names:
            resolved = await resolve_crm_entity(client, entity)
            userfields = await list_crm_userfields(client, resolved)
            exported[entity] = {
                'resolved': {
                    key: value
                    for key, value in resolved.items()
                    if key not in {'fields', 'userfield_list', 'userfield_get'}
                },
                'userFields': [normalize_userfield(field, include_enums=include_enums) for field in userfields],
            }
        return {'entities': exported}

    @server.tool
    async def crm_describe_fields(
        entities: list[str] | str,
        fieldNames: list[str] | str | None = None,
        includeEnums: bool = True,
    ) -> dict[str, Any]:
        """Describe CRM fields and user fields for static entities and smart processes."""
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
            )
        return {'entities': described}

    return server


def main() -> None:
    settings = Settings.from_env()
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
