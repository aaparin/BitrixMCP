from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

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
            'First inspect the Bitrix24 REST API documentation through the MCP documentation tools. '
            'Then call Bitrix24 REST through the call_bitrix_rest tool with the method name and JSON parameters. '
            'Never answer with only a plan, pseudo-code, or suggested REST calls. '
            'For account-data questions, you must execute call_bitrix_rest and answer from its result. '
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

    @agent.tool
    async def call_bitrix_rest(
        ctx: RunContext[AgentDeps],
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Call a read-only Bitrix24 REST API method.

        Args:
            method: Exact Bitrix24 REST method name in dot notation, for example user.get, tasks.task.list, crm.deal.list.
            params: JSON parameters for this method. Keep responses small by using select/filter/order/start.
        """
        logfire.info('Calling Bitrix24 REST API', bitrix_method=method)
        ctx.deps.bitrix_call_count += 1
        return await ctx.deps.bitrix.call_method(method, params or {})

    @agent.output_validator
    async def require_bitrix_call(ctx: RunContext[AgentDeps], output: str) -> str:
        if ctx.deps.bitrix_call_count == 0:
            raise ModelRetry(
                'You tried to answer without calling Bitrix24 REST. '
                'Call call_bitrix_rest with a real Bitrix24 REST method first, then answer from the returned data. '
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
        ]
        if ctx.deps.bitrix_call_count < 4 and any(marker in output.lower() for marker in weak_answer_markers):
            raise ModelRetry(
                'The answer is incomplete. Do not ask the user to make API calls. '
                'Make the missing Bitrix24 REST calls yourself. '
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
    if force_openrouter:
        if not settings.openrouter_api_key:
            return 'OpenRouter was requested explicitly, but OPENROUTER_API_KEY is not configured.'
        logfire.info('Running ask_bitrix directly with OpenRouter by request directive')
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
