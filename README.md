# BitrixMCP

Deterministic MCP server for Bitrix24 REST. No internal LLM: the client model
discovers endpoints and calls curated tools or `bitrix_call`.

## Tools + resources

Discovery:

- `bitrix_search_methods(query, scope?, access?, includeDeprecated?, limit?)`
- `bitrix_describe_method(method)`
- `bitrix_list_scopes()`

Identity:

- `bitrix_whoami()` — signed-in Bitrix24 user (email, id, name) and auth mode

CRM statuses (shared loader + cache):

- Resource `bitrix://crm/statuses` — full dictionary index by `ENTITY_ID`
- Resource template `bitrix://crm/statuses/{entity_id}` — one group (`STATUS`, `DEAL_STAGE`, …)
- Tool `crm_statuses_list(entity_id?, force_refresh?)` — same data for explicit tool calls

Curated reads:

- `crm_types_list` / `crm_items_list` / `crm_item_get` / `crm_count` / `crm_describe_fields`
- `tasks_list` / `task_get` / `tasks_count` / `tasks_list_for_employee`
- `activities_list` / `activity_get`
- `employees_list` / `employee_get` / `employees_search`
- `telephony_calls_list` / `telephony_lines_list`

Curated writes (default `dry_run=true`):

- `crm_item_add` / `crm_item_update`
- `task_add` / `task_update`
- `activity_add`

Escape hatch:

- `bitrix_call(method, params)` — any REST method allowed by `MethodPolicy`

### Breaking changes

- Removed: `ask_bitrix`, `list_capabilities`, `crm_userfield_lookup`, `crm_userfields_export`
- Removed: `bitrix_authorize`, `bitrix_revoke`, `MCP_BEARER_TOKEN`, `X-Bitrix-User-Email` /
  `BITRIX_USER_EMAIL_HEADER` — authentication is OAuth 2.1 for MCP clients (see below)
- Renamed: `call_bitrix_rest` → `bitrix_call`
- `crm_describe_fields` now covers user-field lookup/export via `fieldNames` and `includeSystemFields=false`
- Access control uses a method catalog + `BITRIX_ALLOWED_ACCESS` instead of suffix heuristics
- LLM / OpenRouter / docs-MCP settings were removed

## Configuration

```bash
cp .env.example .env
```

Required:

```dotenv
BITRIX_WEBHOOK_URL="https://example.bitrix24.ru/rest/1/xxxxxxxxxxxxxxx/"
```

Access policy:

```dotenv
BITRIX_ALLOWED_ACCESS="read"
BITRIX_ALLOW_UNKNOWN_METHODS="false"
```

`BITRIX_READ_ONLY` still works for one release when `BITRIX_ALLOWED_ACCESS` is unset
(`true` → `read`, `false` → `read,write,destructive`) and emits a deprecation warning.

Other optional settings: `BITRIX_METHOD_CATALOG_PATH`, `BITRIX_PORTAL_METHODS_TTL_SECONDS`,
`BITRIX_CRM_METADATA_TTL_SECONDS`, `BITRIX_REQUEST_TIMEOUT_SECONDS`, MCP host/port/transport/path,
`MCP_PUBLIC_BASE_URL`, Logfire flags.

## Authentication (OAuth for Claude connectors)

See the full production checklist: [docs/deploy.md](docs/deploy.md).

The server is its own OAuth 2.1 authorization server for MCP clients (DCR, PKCE,
RFC 8414/9728 metadata) and proxies sign-in to a Bitrix24 local application
(FastMCP `OAuthProxy`). Each request carries the user's Bitrix24 token, so **reads
and writes run as the signed-in user** with their portal permissions. The webhook
is only used for method discovery.

```dotenv
BITRIX_OAUTH_ENABLED="true"
BITRIX_OAUTH_CLIENT_ID="..."
BITRIX_OAUTH_CLIENT_SECRET="..."
BITRIX_PORTAL_URL="https://example.bitrix24.ru"
BITRIX_TOKEN_ENCRYPTION_KEY="..."   # Fernet key
BITRIX_TOKEN_DB_PATH="/data/tokens.db"
BITRIX_ALLOWED_ACCESS="read,write"
MCP_PUBLIC_BASE_URL="https://bitrix-mcp.example.com"
MCP_TRANSPORT="streamable-http"
MCP_PATH="/mcp"
```

Bitrix24 local application (with UI): handler URL `https://<MCP_PUBLIC_BASE_URL>/oauth/callback`.
In Claude: **Settings → Connectors → Add custom connector** → `https://<host>/mcp`.

Portal `member_id` is pinned on the first sign-in. OAuth proxy state (client
registrations, encrypted Bitrix24 tokens) lives in `BITRIX_OAUTH_STORAGE_DIR`
(default: `oauth-proxy/` next to the token DB).

Generate an encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

With OAuth on:

- Webhook writes are blocked unless `BITRIX_ALLOW_WEBHOOK_WRITES=true` (emergency only).
- `OwnershipGuard` allows mutating only records the user is responsible for
  (`BITRIX_OWNERSHIP_ADMIN_EMAILS` bypasses ownership, not OAuth).

With `BITRIX_OAUTH_ENABLED=false` the endpoint is unauthenticated and runs as the
webhook — local development only.

## Method catalog

The packaged catalog is generated from [b24-rest-docs](https://github.com/bitrix-tools/b24-rest-docs)
plus `scripts/catalog_data/overrides.json`:

```bash
uv run python scripts/generate_method_catalog.py --docs-ref main --report --strict
```

Policy never calls the network: the static catalog decides allow/deny. Portal
`methods` / `scope` only annotate discovery (`available`) and error text.

## Run

```bash
uv sync
uv run python main.py
```

Docker:

```bash
docker compose up --build
# or production-style:
docker compose -f docker-compose.server.yml up --build
```

## Design notes

- Unknown / undocumented methods are denied by default (`BITRIX_ALLOW_UNKNOWN_METHODS` can open the valve).
- Deprecated methods are allowed with a warning.
- `batch` subcommands are checked individually; nested `batch` and write subcommands are denied.
- `BitrixIdentity` covers webhook + per-user OAuth; writes never use the webhook under OAuth mode.
