from __future__ import annotations

import logfire
from fastmcp import Context, FastMCP

from bitrix_mcp.agent import DOCS_CACHE, answer_question
from bitrix_mcp.config import Settings


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

    server = FastMCP(
        name='Bitrix24 AI MCP',
        instructions=(
            'Use ask_bitrix(question) to answer natural-language questions about the configured Bitrix24 account. '
            'Send questions in English when possible. If another language is received, the server translates it '
            'internally and still answers in English. '
            'To force the stronger OpenRouter model for a single request, start the question with "use openrouter:". '
            'The server keeps Bitrix24 credentials private and defaults to read-only REST methods.'
        ),
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
            'It answers English natural-language questions by consulting Bitrix24 REST API docs '
            'through the configured documentation MCP server and then calling Bitrix24 REST API. '
            'Non-English questions are translated internally and answered in English. '
            'MVP domains: users, tasks, CRM, calendar, and other read-only REST methods available to the webhook. '
            'The server uses a local OpenAI-compatible LLM first and automatically retries the same request with OpenRouter '
            'when the local model cannot complete the agent workflow. '
            'Prefix a question with "use openrouter:" to skip Ollama for that one request.'
        )

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
