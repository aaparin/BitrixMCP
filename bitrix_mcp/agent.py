from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal

import logfire
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.mcp import CallToolFunc, MCPToolset, ToolResult
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.usage import UsageLimits

from bitrix_mcp.bitrix import BitrixApiError, BitrixClient, ReadOnlyViolation
from bitrix_mcp.cache import TTLCache
from bitrix_mcp.config import Settings
from bitrix_mcp.crm_metadata import CrmFilterResolutionError, CrmMetadataResolver


OPENROUTER_DIRECTIVES = (
    'use openrouter:',
    'use cloud:',
    '[openrouter]',
    '[cloud]',
)


@dataclass
class AgentDeps:
    bitrix: BitrixClient
    bitrix_call_count: int = 0


DOCS_CACHE = TTLCache(ttl_seconds=3600)

CRM_ENTITY_METHODS = {
    'company': 'crm.company.list',
    'contact': 'crm.contact.list',
    'deal': 'crm.deal.list',
    'lead': 'crm.lead.list',
}

COMPACT_LIST_SELECTS = {
    'crm.company.list': ['ID', 'TITLE', 'ASSIGNED_BY_ID'],
    'crm.contact.list': ['ID', 'NAME', 'LAST_NAME', 'ASSIGNED_BY_ID'],
    'crm.deal.list': ['ID', 'TITLE', 'STAGE_ID', 'STAGE_SEMANTIC_ID', 'CLOSEDATE', 'OPPORTUNITY', 'CURRENCY_ID'],
    'crm.lead.list': ['ID', 'TITLE', 'STATUS_ID', 'ASSIGNED_BY_ID'],
    'tasks.task.list': ['ID', 'TITLE', 'STATUS', 'RESPONSIBLE_ID', 'CREATED_DATE', 'DEADLINE'],
}

TASK_STATUS_ALIASES = {
    'new': 1,
    'pending': 2,
    'accepted': 2,
    'in_progress': 3,
    'in progress': 3,
    'active': 3,
    'current': 3,
    'almost completed': 4,
    'awaiting review': 4,
    'completed': 5,
    'finished': 5,
    'done': 5,
    'deferred': 6,
}


def compact_records(value: Any, *, limit: int = 10) -> Any:
    records = extract_records(value)
    if records is None:
        return value
    compacted = records[:limit]
    if len(records) > limit:
        return {
            'items': compacted,
            'truncated': True,
            'returned': limit,
            'available_in_response': len(records),
        }
    return compacted


def extract_records(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ['tasks', 'items', 'companies', 'contacts', 'deals', 'leads']:
            items = value.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return None


def first_record(value: Any) -> dict[str, Any] | None:
    records = extract_records(value)
    if records:
        return records[0]
    return None


def user_display_name(user: dict[str, Any] | None) -> str | None:
    if not user:
        return None
    name = ' '.join(
        part.strip()
        for part in [str(user.get('NAME') or ''), str(user.get('LAST_NAME') or '')]
        if part and part.strip()
    )
    return name or user.get('EMAIL') or user.get('ID')


def user_matches_query(user: dict[str, Any], query: str) -> bool:
    normalized_query = normalize_text(query)
    values = [
        user.get('NAME'),
        user.get('LAST_NAME'),
        user.get('EMAIL'),
        user_display_name(user),
    ]
    normalized_values = [normalize_text(str(value)) for value in values if value]
    return any(normalized_query in value or value in normalized_query for value in normalized_values)


def build_user_search_filters(query: str) -> list[dict[str, Any]]:
    parts = [part for part in query.strip().split() if part]
    filters: list[dict[str, Any]] = []
    if len(parts) >= 2:
        first, last = parts[0], ' '.join(parts[1:])
        filters.append({'NAME': first, 'LAST_NAME': last})
        filters.append({'NAME': last, 'LAST_NAME': first})
    if parts:
        filters.append({'NAME': parts[0]})
        filters.append({'LAST_NAME': parts[-1]})
    if '@' in query:
        filters.append({'EMAIL': query})
    return filters or [{'NAME': query}]


def dedupe_users(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for user in users:
        user_id = str(user.get('ID') or '')
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        deduped.append(user)
    return deduped


def normalize_text(value: str) -> str:
    return ' '.join(value.strip().lower().split())


def parse_filter_input(filter_value: dict[str, Any] | str | None) -> dict[str, Any]:
    if filter_value is None:
        return {}
    if isinstance(filter_value, dict):
        return dict(filter_value)
    if isinstance(filter_value, str):
        try:
            parsed = json.loads(filter_value)
        except json.JSONDecodeError as exc:
            raise ModelRetry('Filter must be a JSON object, not a plain string.') from exc
        if isinstance(parsed, dict):
            return parsed
    raise ModelRetry('Filter must be an object.')


def parse_params_input(params_value: dict[str, Any] | str | None) -> dict[str, Any]:
    if params_value is None:
        return {}
    if isinstance(params_value, dict):
        return dict(params_value)
    if isinstance(params_value, str):
        try:
            parsed = json.loads(params_value)
        except json.JSONDecodeError as exc:
            raise ModelRetry('REST params must be a JSON object, not a plain string.') from exc
        if isinstance(parsed, dict):
            return parsed
    raise ModelRetry('REST params must be an object.')


def parse_conditions_input(conditions: list[dict[str, Any]] | str | None) -> list[dict[str, Any]]:
    if conditions is None:
        return []
    if isinstance(conditions, list):
        return conditions
    if isinstance(conditions, str):
        try:
            parsed = json.loads(conditions)
        except json.JSONDecodeError as exc:
            raise ModelRetry('Conditions must be a JSON array of objects, not a plain string.') from exc
        if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
            return parsed
    raise ModelRetry('Conditions must be an array of objects.')


def normalize_task_status_filter(status: str | int | None) -> dict[str, Any]:
    if status is None:
        return {}
    if isinstance(status, int):
        return {'STATUS': status}
    normalized = normalize_text(status)
    if normalized in {'open', 'not completed', 'non completed', 'not done'}:
        return {'!STATUS': 5}
    if normalized in TASK_STATUS_ALIASES:
        return {'STATUS': TASK_STATUS_ALIASES[normalized]}
    if status.isdigit():
        return {'STATUS': int(status)}
    return {'STATUS': status}


def looks_like_raw_count_request(method: str, params: dict[str, Any] | None) -> bool:
    method = method.lower()
    if method not in {*CRM_ENTITY_METHODS.values(), 'tasks.task.list'}:
        return False
    params = params or {}
    select = params.get('select') or params.get('SELECT')
    return (
        params.get('count') is True
        or params.get('COUNT') is True
        or params.get('count_only') is True
        or params.get('COUNT_ONLY') in {True, 'Y', 'y', '1', 1}
        or params.get('total') is True
        or params.get('TOTAL') is True
        or select == ['ID']
        or select == ['id']
    )


def compact_raw_list_params(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(params or {})
    method = method.lower()
    if method not in COMPACT_LIST_SELECTS:
        return normalized

    if 'FILTER' in normalized and 'filter' not in normalized:
        normalized['filter'] = normalized.pop('FILTER')
    if 'SELECT' in normalized and 'select' not in normalized:
        normalized['select'] = normalized.pop('SELECT')
    if 'ORDER' in normalized and 'order' not in normalized:
        normalized['order'] = normalized.pop('ORDER')

    normalized.setdefault('select', COMPACT_LIST_SELECTS[method].copy())
    normalized.setdefault('start', 0)
    return normalized


def sanitize_for_json(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode('utf-8', errors='replace').decode('utf-8')
    if isinstance(value, dict):
        return {
            sanitize_for_json(key): sanitize_for_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_for_json(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_json(item) for item in value)
    return value


class SanitizedMCPToolset(MCPToolset[Any]):
    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        tools = await super().get_tools(ctx)
        return {
            name: replace(
                tool,
                tool_def=replace(
                    tool.tool_def,
                    description=sanitize_for_json(tool.tool_def.description),
                    parameters_json_schema=sanitize_for_json(tool.tool_def.parameters_json_schema),
                    metadata=sanitize_for_json(tool.tool_def.metadata),
                    return_schema=sanitize_for_json(tool.tool_def.return_schema),
                ),
            )
            for name, tool in tools.items()
        }


async def cached_docs_tool_call(
    ctx: RunContext[Any],
    call_tool: CallToolFunc,
    name: str,
    args: dict[str, Any],
) -> ToolResult:
    cache_key = DOCS_CACHE.make_key(name, args)
    cached = DOCS_CACHE.get(cache_key)
    if cached is not None:
        logfire.info('Bitrix docs MCP cache hit', tool_name=name)
        return cached

    logfire.info('Calling Bitrix docs MCP tool', tool_name=name)
    result = await call_tool(name, args)
    result = sanitize_for_json(result)
    DOCS_CACHE.set(cache_key, result)
    return result


def build_local_model(settings: Settings) -> OpenAIChatModel:
    return LocalOpenAIChatModel(
        settings.local_llm_model,
        provider=OpenAIProvider(
            base_url=settings.local_llm_base_url,
            api_key=settings.local_llm_api_key,
        ),
    )


class LocalOpenAIChatModel(OpenAIChatModel):
    class _MapModelResponseContext(OpenAIChatModel._MapModelResponseContext):
        def _into_message_param(self):
            message_param = super()._into_message_param()
            if (
                message_param is not None
                and message_param.get('content') is None
                and message_param.get('tool_calls')
            ):
                message_param['content'] = ''
            return message_param


def build_cloud_model(settings: Settings) -> OpenRouterModel:
    return OpenRouterModel(
        settings.openrouter_model,
        provider=OpenRouterProvider(
            api_key=settings.openrouter_api_key,
            app_title='BitrixMCP',
        ),
    )


def build_agent(settings: Settings, *, use_cloud: bool = False) -> Agent[AgentDeps, str]:
    model = build_cloud_model(settings) if use_cloud else build_local_model(settings)
    model_label = 'OpenRouter fallback' if use_cloud else 'local OpenAI-compatible LLM'
    docs_toolset = SanitizedMCPToolset(
        settings.docs_mcp_url,
        id='bitrix24-docs',
        process_tool_call=cached_docs_tool_call,
        cache_tools=True,
        include_instructions=True,
        init_timeout=5,
        read_timeout=settings.request_timeout_seconds,
    )

    agent = Agent(
        model,
        deps_type=AgentDeps,
        output_type=str,
        toolsets=[docs_toolset],
        instructions=(
            f'You answer questions about one Bitrix24 account by using tools. Current model: {model_label}. '
            'For CRM and task questions, prefer the typed CRM/task tools over raw REST calls. '
            'Use documentation tools and call_bitrix_rest only when the typed tools cannot cover the request. '
            'Never answer with only a plan, pseudo-code, or suggested REST calls. '
            'For account-data questions, you must execute at least one Bitrix24 tool and answer from its result. '
            'For CRM count questions, use count_crm_entities; when the count has conditions, pass them in filter. '
            'For task count questions, use count_tasks; when the count has conditions, pass them in filter. '
            'Do not paginate manually for count questions. '
            'For task questions about a named employee or user, use list_tasks_for_user or search_users first; do not search CRM contacts. '
            'For CRM contacts, the responsible user field is ASSIGNED_BY_ID. '
            'To answer who is responsible for a contact, find the contact with crm.contact.list selecting ASSIGNED_BY_ID, '
            'then call user.get for that user ID. '
            'For CRM companies, the display field is TITLE, not NAME. '
            'To answer who is responsible for a company, find the company with crm.company.list using filter {"=TITLE": "..."} '
            'and select ["ID", "TITLE", "ASSIGNED_BY_ID"], then call user.get for ASSIGNED_BY_ID. '
            'For user.get, params.filter must be an object such as {"ID": 456}, not a string like "ID=456". '
            'For user.get, select ["ID", "NAME", "LAST_NAME", "EMAIL"]. '
            'If the first API result misses a needed field, make another API call with a better select/filter instead of asking the user. '
            'Use only read-only API methods. Do not ask for or reveal tokens, webhook URLs, or credentials. '
            'If the question cannot be answered, explain the reason clearly. '
            'Use English as the only working language. '
            'If the user question is not in English, translate it internally to English before planning, '
            'looking up documentation, creating Bitrix24 REST parameters, and writing the final answer. '
            'Always answer in English. '
            'When useful, include compact tables or bullet lists.'
        ),
    )

    async def tracked_bitrix_call(ctx: RunContext[AgentDeps], method: str, params: dict[str, Any] | None = None) -> Any:
        logfire.info('Calling Bitrix24 REST API', bitrix_method=method)
        ctx.deps.bitrix_call_count += 1
        return await ctx.deps.bitrix.call_method(method, params or {})

    async def tracked_bitrix_count(ctx: RunContext[AgentDeps], method: str, params: dict[str, Any] | None = None) -> int:
        logfire.info('Counting Bitrix24 REST list', bitrix_method=method)
        ctx.deps.bitrix_call_count += 1
        return await ctx.deps.bitrix.count_list_method(method, params or {})

    @agent.tool
    async def search_users(ctx: RunContext[AgentDeps], query: str, limit: int = 10) -> Any:
        """Search Bitrix24 employees/users by name or email and return compact identity fields."""
        users: list[dict[str, Any]] = []
        for filter_ in build_user_search_filters(query):
            result = await tracked_bitrix_call(
                ctx,
                'user.get',
                {'filter': filter_, 'select': ['ID', 'NAME', 'LAST_NAME', 'EMAIL', 'ACTIVE']},
            )
            records = extract_records(result) or []
            users.extend(user for user in records if user_matches_query(user, query))

        users = dedupe_users(users)
        return compact_records(users, limit=max(1, min(limit, 20)))

    @agent.tool
    async def get_user_by_id(ctx: RunContext[AgentDeps], user_id: int) -> dict[str, Any]:
        """Get one Bitrix24 user by numeric ID with compact identity fields."""
        result = await tracked_bitrix_call(
            ctx,
            'user.get',
            {'filter': {'ID': user_id}, 'select': ['ID', 'NAME', 'LAST_NAME', 'EMAIL']},
        )
        user = first_record(result)
        if user is None:
            return {'found': False, 'user_id': user_id}
        return {'found': True, 'user': user}

    @agent.tool
    async def search_crm_companies(
        ctx: RunContext[AgentDeps],
        title: str,
        exact: bool = True,
        limit: int = 10,
    ) -> Any:
        """Find CRM companies by TITLE. Use for company questions."""
        filter_key = '=TITLE' if exact else '%TITLE'
        result = await tracked_bitrix_call(
            ctx,
            'crm.company.list',
            {
                'filter': {filter_key: title},
                'select': ['ID', 'TITLE', 'ASSIGNED_BY_ID'],
                'start': 0,
            },
        )
        return compact_records(result, limit=max(1, min(limit, 20)))

    @agent.tool
    async def get_company_responsible(ctx: RunContext[AgentDeps], title: str) -> dict[str, Any]:
        """Find a CRM company by exact TITLE and return its responsible user."""
        companies = await tracked_bitrix_call(
            ctx,
            'crm.company.list',
            {
                'filter': {'=TITLE': title},
                'select': ['ID', 'TITLE', 'ASSIGNED_BY_ID'],
                'start': 0,
            },
        )
        company = first_record(companies)
        if company is None:
            return {'found': False, 'company_title': title}

        assigned_by_id = company.get('ASSIGNED_BY_ID')
        if not assigned_by_id:
            return {'found': True, 'company': company, 'responsible_found': False}

        users = await tracked_bitrix_call(
            ctx,
            'user.get',
            {
                'filter': {'ID': int(assigned_by_id)},
                'select': ['ID', 'NAME', 'LAST_NAME', 'EMAIL'],
            },
        )
        user = first_record(users)
        return {
            'found': True,
            'company': company,
            'responsible_found': user is not None,
            'responsible': user,
            'responsible_name': user_display_name(user),
        }

    @agent.tool
    async def search_crm_contacts(
        ctx: RunContext[AgentDeps],
        name: str,
        limit: int = 10,
    ) -> Any:
        """Find CRM contacts by name fragment and include responsible user ID."""
        result = await tracked_bitrix_call(
            ctx,
            'crm.contact.list',
            {
                'filter': {'%NAME': name},
                'select': ['ID', 'NAME', 'LAST_NAME', 'ASSIGNED_BY_ID'],
                'start': 0,
            },
        )
        return compact_records(result, limit=max(1, min(limit, 20)))

    @agent.tool
    async def search_crm_deals(
        ctx: RunContext[AgentDeps],
        title: str,
        exact: bool = True,
        limit: int = 10,
    ) -> Any:
        """Find CRM deals by TITLE and include amount, currency, stage, and responsible user."""
        filter_key = '=TITLE' if exact else '%TITLE'
        result = await tracked_bitrix_call(
            ctx,
            'crm.deal.list',
            {
                'filter': {filter_key: title},
                'select': ['ID', 'TITLE', 'OPPORTUNITY', 'CURRENCY_ID', 'STAGE_ID', 'ASSIGNED_BY_ID'],
                'start': 0,
            },
        )
        return compact_records(result, limit=max(1, min(limit, 20)))

    @agent.tool
    async def count_crm_entities(
        ctx: RunContext[AgentDeps],
        entity: Literal['company', 'contact', 'deal', 'lead'],
        filter: dict[str, Any] | str | None = None,
        conditions: list[dict[str, Any]] | str | None = None,
    ) -> dict[str, Any]:
        """Count CRM companies, contacts, deals, or leads using Bitrix24 total without pagination.

        Args:
            entity: CRM entity to count.
            filter: Bitrix24 filter. Conditions can use human aliases such as
                {"status": "won", "year": 2026}; the server resolves CRM fields
                and dictionary values through cached Bitrix24 metadata.
            conditions: Optional structured conditions, for example
                [{"field": "status", "value": "won"}, {"field": "close date", "operator": "year", "value": 2026}].
        """
        method = CRM_ENTITY_METHODS[entity]
        resolver = CrmMetadataResolver(ctx.deps.bitrix)
        try:
            normalized_filter = await resolver.resolve_filter(
                entity,
                filter=parse_filter_input(filter),
                conditions=parse_conditions_input(conditions),
            )
        except CrmFilterResolutionError as exc:
            raise ModelRetry(str(exc)) from exc
        params = {'filter': normalized_filter, 'select': ['ID'], 'start': 0}
        total = await tracked_bitrix_count(ctx, method, params)
        return {'entity': entity, 'filter': normalized_filter, 'total': total}

    @agent.tool
    async def count_tasks(
        ctx: RunContext[AgentDeps],
        filter: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        """Count Bitrix24 tasks using total without pagination."""
        parsed_filter = parse_filter_input(filter)
        params = {'filter': parsed_filter, 'select': ['ID'], 'start': 0}
        total = await tracked_bitrix_count(ctx, 'tasks.task.list', params)
        return {'entity': 'task', 'filter': parsed_filter, 'total': total}

    @agent.tool
    async def list_tasks_for_user(
        ctx: RunContext[AgentDeps],
        user_name: str,
        status: str | int | None = 'open',
        limit: int = 10,
    ) -> Any:
        """List tasks where a named Bitrix24 employee/user is responsible.

        Args:
            user_name: Bitrix24 employee/user name or email.
            status: Task status. Defaults to "open", meaning not completed. Use "completed" for finished tasks or None for all tasks.
            limit: Maximum number of tasks to return.
        """
        users_result = await search_users(ctx, user_name, limit=5)
        users = extract_records(users_result) or []
        if not users:
            return {'found': False, 'user_name': user_name, 'tasks': []}
        if len(users) > 1:
            return {
                'found': False,
                'ambiguous': True,
                'user_name': user_name,
                'candidates': users,
            }

        user = users[0]
        filter_: dict[str, Any] = {'RESPONSIBLE_ID': user['ID']}
        filter_.update(normalize_task_status_filter(status))

        tasks = await tracked_bitrix_call(
            ctx,
            'tasks.task.list',
            {
                'filter': filter_,
                'select': ['ID', 'TITLE', 'STATUS', 'RESPONSIBLE_ID', 'CREATED_DATE', 'DEADLINE'],
                'start': 0,
            },
        )
        return {
            'found': True,
            'user': user,
            'tasks': compact_records(tasks, limit=max(1, min(limit, 20))),
        }

    @agent.tool
    async def list_tasks(
        ctx: RunContext[AgentDeps],
        filter: dict[str, Any] | str | None = None,
        limit: int = 10,
    ) -> Any:
        """List a compact page of Bitrix24 tasks. Use filters to keep the result small."""
        parsed_filter = parse_filter_input(filter)
        result = await tracked_bitrix_call(
            ctx,
            'tasks.task.list',
            {
                'filter': parsed_filter,
                'select': ['ID', 'TITLE', 'STATUS', 'RESPONSIBLE_ID', 'CREATED_DATE', 'DEADLINE'],
                'start': 0,
            },
        )
        return compact_records(result, limit=max(1, min(limit, 20)))

    @agent.tool
    async def call_bitrix_rest(
        ctx: RunContext[AgentDeps],
        method: str,
        params: dict[str, Any] | str | None = None,
    ) -> Any:
        """Call a read-only Bitrix24 REST API method.

        Args:
            method: Exact Bitrix24 REST method name in dot notation, for example user.get, tasks.task.list, crm.deal.list.
            params: JSON parameters for this method. Keep responses small by using select/filter/order/start.
        """
        parsed_params = parse_params_input(params)
        if looks_like_raw_count_request(method, parsed_params):
            raise ModelRetry(
                'This is a count request. Use count_crm_entities for CRM counts or count_tasks for task counts. '
                'When the count has conditions, pass them in the filter argument.'
            )
        result = await tracked_bitrix_call(ctx, method, compact_raw_list_params(method, parsed_params))
        return compact_records(result, limit=20)

    @agent.output_validator
    async def require_bitrix_call(ctx: RunContext[AgentDeps], output: str) -> str:
        if ctx.deps.bitrix_call_count == 0:
            raise ModelRetry(
                'You tried to answer without calling Bitrix24 REST. '
                'Call a typed CRM/task tool or call_bitrix_rest first, then answer from the returned data. '
                'Do not return a plan or example JSON.'
            )
        weak_answer_markers = [
            'необходимо дополнительно',
            'не указано',
            'нужно дополнительно',
            'уточните',
            'укажите',
            'i need more information',
            'please provide',
            'i cannot access',
            'looks like you',
            '</think>',
            'cannot determine',
            'need to',
            'truncated',
            '20+',
        ]
        if ctx.deps.bitrix_call_count < 4 and any(marker in output.lower() for marker in weak_answer_markers):
            raise ModelRetry(
                'The answer is incomplete. Do not ask the user to make API calls. '
                'Make the missing Bitrix24 REST calls yourself. '
                'For count questions, use count_crm_entities or count_tasks. '
                'When the count has conditions, pass them in the filter argument. '
                'For task questions about a named employee or user, use search_users or list_tasks_for_user, not CRM contacts. '
                'For CRM contact responsible user, use crm.contact.list with ASSIGNED_BY_ID, then user.get.'
            )
        return output

    return agent


WEAK_ANSWER_MARKERS = [
    'не удалось ответить',
    'не удалось обработать',
    'не могу определить',
    'не могу выполнить',
    'необходимо дополнительно',
    'не указано',
    'нужно дополнительно',
    'уточните',
    'укажите',
    'i need more information',
    'please provide',
    'i cannot access',
    'looks like you',
    '</think>',
    '<think>',
    'cannot determine',
    'need to',
    'configuration error',
]


def parse_openrouter_directive(question: str) -> tuple[str, bool]:
    stripped = question.strip()
    lowered = stripped.lower()
    for directive in OPENROUTER_DIRECTIVES:
        if lowered.startswith(directive):
            return stripped[len(directive):].strip(), True
    return question, False


def should_retry_with_cloud(answer: str) -> bool:
    lowered = answer.lower()
    return any(marker in lowered for marker in WEAK_ANSWER_MARKERS)


async def run_agent_once(question: str, settings: Settings, *, use_cloud: bool) -> str:
    bitrix = BitrixClient(
        settings.bitrix_webhook_url,
        timeout=settings.request_timeout_seconds,
        read_only=settings.read_only,
    )
    deps = AgentDeps(bitrix=bitrix)
    agent = build_agent(settings, use_cloud=use_cloud)

    try:
        async with agent:
            result = await agent.run(
                question,
                deps=deps,
                usage_limits=UsageLimits(request_limit=settings.max_agent_steps),
            )
            return result.output
    except ReadOnlyViolation as exc:
        return f'Не могу выполнить запрос: {exc}'
    except BitrixApiError as exc:
        return f'Bitrix24 вернул ошибку: {exc}'
    except UsageLimitExceeded:
        return 'Не удалось ответить за допустимое число агентных шагов. Попробуйте сузить вопрос.'
    except TimeoutError:
        return 'Не удалось ответить за отведенное время: один из внешних сервисов не ответил вовремя.'
    except UnicodeEncodeError:
        return 'Не удалось отправить запрос в LLM из-за невалидной кодировки в данных внешнего MCP-сервера документации.'
    except Exception as exc:
        logfire.exception('Unexpected ask_bitrix failure')
        return f'Не удалось обработать запрос: {type(exc).__name__}: {exc}'


async def answer_question(question: str, settings: Settings) -> str:
    question, force_openrouter = parse_openrouter_directive(question)
    if settings.force_openrouter or force_openrouter:
        if not settings.openrouter_api_key:
            return 'OpenRouter was requested explicitly or forced by config, but OPENROUTER_API_KEY is not configured.'
        logfire.info(
            'Running ask_bitrix directly with OpenRouter',
            forced_by_config=settings.force_openrouter,
            forced_by_directive=force_openrouter,
        )
        return await run_agent_once(question, settings, use_cloud=True)

    local_answer = await run_agent_once(question, settings, use_cloud=False)
    if not settings.openrouter_api_key or not should_retry_with_cloud(local_answer):
        return local_answer

    logfire.info('Retrying ask_bitrix with OpenRouter fallback')
    cloud_answer = await run_agent_once(question, settings, use_cloud=True)
    if should_retry_with_cloud(cloud_answer):
        return (
            'Локальная модель не справилась, OpenRouter fallback тоже не дал уверенный ответ.\n\n'
            f'Ответ OpenRouter: {cloud_answer}'
        )
    return cloud_answer
