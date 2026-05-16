# BitrixMCP

MCP server that answers natural-language questions about one Bitrix24 account.

The server exposes:

- `ask_bitrix(question: str)` - main tool.
- `list_capabilities()` - short capability description for MCP clients.

Internally `ask_bitrix` uses a Pydantic AI agent with a local OpenAI-compatible LLM first, connects to the Bitrix24 documentation MCP server, then calls the configured Bitrix24 REST webhook in read-only mode. OpenRouter is used only as an automatic or explicit fallback.

## Configuration

Create `.env` from the example:

```bash
cp .env.example .env
```

Then fill in the required values:

```dotenv
BITRIX_WEBHOOK_URL="https://example.bitrix24.ru/rest/1/xxxxxxxxxxxxxxx/"
```

Optional:

```dotenv
LOCAL_LLM_BASE_URL="http://127.0.0.1:1234/v1"
LOCAL_LLM_MODEL="gemma-4-e4b-it"
LOCAL_LLM_API_KEY="api-key-not-set"
OPENROUTER_API_KEY="..."
OPENROUTER_MODEL="qwen/qwen3-32b"
BITRIX_DOCS_MCP_URL="https://mcp-dev.bitrix24.com/mcp"
BITRIX_READ_ONLY="true"
BITRIX_DOCS_CACHE_TTL_SECONDS="3600"
BITRIX_AGENT_MAX_STEPS="12"
BITRIX_REQUEST_TIMEOUT_SECONDS="20"
MCP_HOST="127.0.0.1"
MCP_PORT="8000"
LOGFIRE_ENABLED="true"
LOGFIRE_INSTRUMENT_HTTPX="false"
```

Exported shell variables have priority over `.env` values.

`LOGFIRE_INSTRUMENT_HTTPX` is disabled by default because Bitrix24 webhook tokens are part of the request URL. The server still logs safe Bitrix method names manually.

## LLM Fallback

Every request starts with the local OpenAI-compatible LLM. If the local model cannot complete the agent workflow, returns a weak answer, fails to build a useful Bitrix24 REST call, or hits the step limit, the same question is retried automatically with OpenRouter.

`OPENROUTER_API_KEY` is optional. If it is missing, the server returns the local model result without fallback.

To skip the local LLM for one request, prefix the question:

```text
use openrouter: who is responsible for contact Qiusi Dong?
```

Accepted prefixes are `use openrouter:`, `use cloud:`, `[openrouter]`, and `[cloud]`.

## Language

The MCP-facing contract is English. Send questions in English when possible. If a non-English question arrives, the agent is instructed to translate it internally to English before planning, documentation lookup, REST calls, and final answer. Final answers are always in English.

## Run

```bash
uv run python main.py
```

The server starts an SSE MCP endpoint at:

```text
http://127.0.0.1:8000/sse
```

Use that URL in Claude Desktop, Cursor, or another MCP client that supports remote/SSE servers.

## Manual Test

Run the same agent flow without an MCP client:

```bash
uv run python scripts/ask.py "how many active users do we have?"
```

Or run it interactively:

```bash
uv run python scripts/ask.py
```

Force OpenRouter in the manual script:

```bash
uv run python scripts/ask.py "use openrouter: who is responsible for contact Qiusi Dong?"
```

## Docker

Build and run:

```bash
docker compose up --build -d
```

The container exposes:

```text
http://127.0.0.1:8000/sse
```

When the MCP server runs in Docker and LM Studio runs on the host machine, use this in `.env`:

```dotenv
LOCAL_LLM_BASE_URL="http://host.docker.internal:1234/v1"
MCP_HOST="0.0.0.0"
```

For Ollama, use:

```dotenv
LOCAL_LLM_BASE_URL="http://host.docker.internal:11434/v1"
LOCAL_LLM_MODEL="qwen3:8b"
```

For nginx reverse proxy, keep SSE buffering disabled, for example:

```nginx
location /sse {
    proxy_pass http://127.0.0.1:8000/sse;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
}
```

## Safety

`BITRIX_READ_ONLY=true` blocks methods whose names include write actions such as `add`, `update`, `delete`, `set`, `bind`, `unbind`, `send`, `import`, or `batch`.
