# BitrixMCP

MCP server that answers natural-language questions about one Bitrix24 account.

The server exposes:

- `ask_bitrix(question: str)` - main tool.
- `list_capabilities()` - short capability description for MCP clients.
- `crm_types_list(...)` - smart-process types and their `entityTypeId` values.
- `crm_items_list(...)` / `crm_item_get(...)` - universal CRM reads for standard entities and smart processes.
- `tasks_list(...)` / `task_get(...)` - task list and detail reads.
- `activities_list(...)` / `activity_get(...)` - CRM activity list and detail reads.
- `employees_list(...)` / `employee_get(...)` - employee/user reads.
- `telephony_calls_list(...)` / `telephony_lines_list()` - call statistics and safe outgoing-line metadata.
- `call_bitrix_rest(method, params)` - direct read-only Bitrix24 REST call without LLM planning.
- `crm_userfield_lookup(entity, field_names, include_enums)` - compact lookup for CRM user fields by `FIELD_NAME`.
- `crm_userfields_export(entities, include_enums)` - compact export of all CRM user fields for selected entities.
- `crm_describe_fields(entities, fieldNames, includeEnums)` - compact CRM field description for static CRM entities and smart processes.

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
BITRIX_FORCE_OPENROUTER="false"
BITRIX_DOCS_MCP_URL="https://mcp-dev.bitrix24.com/mcp"
BITRIX_READ_ONLY="true"
BITRIX_DOCS_CACHE_TTL_SECONDS="3600"
BITRIX_AGENT_MAX_STEPS="12"
BITRIX_REQUEST_TIMEOUT_SECONDS="20"
MCP_HOST="127.0.0.1"
MCP_PORT="8000"
MCP_TRANSPORT="sse"
MCP_PATH="/sse"
MCP_BEARER_TOKEN=""
MCP_PUBLIC_BASE_URL=""
LOGFIRE_ENABLED="true"
LOGFIRE_INSTRUMENT_HTTPX="false"
```

Exported shell variables have priority over `.env` values.

`LOGFIRE_INSTRUMENT_HTTPX` is disabled by default because Bitrix24 webhook tokens are part of the request URL. The server still logs safe Bitrix method names manually.

## Authentication

For public deployments, set a shared bearer token:

```dotenv
MCP_BEARER_TOKEN="change-this-long-random-token"
MCP_PUBLIC_BASE_URL="https://bitrix-mcp.example.com"
```

Clients must send:

```http
Authorization: Bearer change-this-long-random-token
```

For Codex-style MCP configuration, set the same token in the client environment and use `MCP_BEARER_TOKEN` as the bearer token environment variable. For Copilot Studio / Microsoft 365 Copilot connectors, use bearer/API-key authentication if available, or add a static header:

```text
Authorization: Bearer change-this-long-random-token
```

This is a shared service token, not per-user auth.

## LLM Fallback

Every request starts with the local OpenAI-compatible LLM. If the local model cannot complete the agent workflow, returns a weak answer, fails to build a useful Bitrix24 REST call, or hits the step limit, the same question is retried automatically with OpenRouter.

`OPENROUTER_API_KEY` is optional. If it is missing, the server returns the local model result without fallback.

To skip the local LLM for all requests, set:

```dotenv
BITRIX_FORCE_OPENROUTER="true"
```

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

For the newer Streamable HTTP transport, set:

```dotenv
MCP_TRANSPORT="streamable-http"
MCP_PATH="/mcp"
```

Then use:

```text
http://127.0.0.1:8000/mcp
```

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

## Direct CRM Metadata Tools

Use these when you need deterministic JSON for scripts or documentation updates.

Lookup specific user fields:

```json
{
  "tool": "crm_userfield_lookup",
  "input": {
    "entity": "DEAL",
    "field_names": ["UF_CRM_1592556962319"],
    "include_enums": true
  }
}
```

Describe fields across entities and smart processes:

```json
{
  "tool": "crm_describe_fields",
  "input": {
    "entities": ["LEAD", "DEAL", "COMPANY", "QUOTE", "DYNAMIC_1032", "DYNAMIC_1036"],
    "fieldNames": ["UF_CRM_1592556962319", "UF_CRM_1620652518"],
    "includeEnums": true
  }
}
```

Export all user fields for selected entities:

```json
{
  "tool": "crm_userfields_export",
  "input": {
    "entities": ["LEAD", "DEAL"],
    "include_enums": true
  }
}
```

These tools call Bitrix24 REST directly and return JSON-compatible objects without Markdown formatting or final LLM prose.

## Deterministic Read Tools

Use the domain read tools instead of `ask_bitrix` when the caller already knows what data it needs.
They return the original Bitrix24 payload, including `total` and `next` pagination metadata when available.
All filters, selects, and order objects may be passed either as native JSON objects/arrays or as JSON strings.

CRM accepts aliases such as `lead`, `deal`, `contact`, `company`, `quote`, and `invoice`,
as well as numeric `entityTypeId` values and strings such as `DYNAMIC_1032`.
Use `crm_types_list` to discover smart-process type IDs.

```json
{
  "tool": "crm_items_list",
  "input": {
    "entity": "deal",
    "filter": {"stageSemanticId": "S"},
    "select": ["id", "title", "opportunity", "currencyId"],
    "order": {"id": "DESC"},
    "start": 0
  }
}
```

```json
{
  "tool": "tasks_list",
  "input": {
    "filter": {"RESPONSIBLE_ID": 7, "!STATUS": 5},
    "order": {"DEADLINE": "ASC"},
    "start": 0
  }
}
```

```json
{
  "tool": "activities_list",
  "input": {
    "filter": {"OWNER_TYPE_ID": 3, "OWNER_ID": 102},
    "select": ["*", "COMMUNICATIONS"],
    "order": {"ID": "DESC"}
  }
}
```

```json
{
  "tool": "telephony_calls_list",
  "input": {
    "filter": {">=CALL_START_DATE": "2026-01-01T00:00:00+02:00"},
    "order": "DESC",
    "start": 0
  }
}
```

No dedicated SIP configuration tool is provided because Bitrix24 includes SIP passwords in those responses.

## Docker

Build and run:

```bash
docker compose up --build -d
```

The container exposes:

```text
http://127.0.0.1:8000/sse
```

If `MCP_TRANSPORT="streamable-http"` and `MCP_PATH="/mcp"` are set, the container exposes:

```text
http://127.0.0.1:8000/mcp
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

For Streamable HTTP:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8000/mcp;
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
