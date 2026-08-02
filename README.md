# BitrixMCP

Deterministic MCP server for Bitrix24 REST. No internal LLM: the client model
discovers endpoints and calls curated tools or `bitrix_call`.

## Tools + resources

Discovery:

- `bitrix_search_methods(query, scope?, access?, includeDeprecated?, limit?)`
- `bitrix_describe_method(method)`
- `bitrix_list_scopes()`

Identity / OAuth:

- `bitrix_whoami()` — current email, Bitrix user id, auth mode, token expiry
- `bitrix_authorize()` — authorization URL (idempotent)
- `bitrix_revoke()` — drop stored user token

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

Curated writes (require OAuth user identity; default `dry_run=true`):

- `crm_item_add` / `crm_item_update`
- `task_add` / `task_update`
- `activity_add`

Escape hatch:

- `bitrix_call(method, params)` — any REST method allowed by `MethodPolicy`

### Breaking changes

- Removed: `ask_bitrix`, `list_capabilities`, `crm_userfield_lookup`, `crm_userfields_export`
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
`MCP_BEARER_TOKEN`, `MCP_PUBLIC_BASE_URL`, Logfire flags.

## Per-user OAuth writes

See the full production checklist: [docs/deploy.md](docs/deploy.md).

Reads stay on the inbound webhook. Writes run as the LibreChat user after a one-time
browser consent. LibreChat must send the authenticated user email in
`X-Bitrix-User-Email` (or `BITRIX_USER_EMAIL_HEADER`).

### Register a local application (with UI)

In Bitrix24: **Applications → Developer resources → Other → Local application**.

1. Do **not** enable “Application uses only API” — register **with an interface**
   (needed for the `/bitrix/app` fallback placement page).
2. Handler / `redirect_uri`: `https://<MCP_PUBLIC_BASE_URL>/oauth/callback`
3. Scopes must cover at least what the webhook can do (crm, task, user, …).
4. Put `client_id` / `client_secret` into env.

### Enable on the server

```dotenv
BITRIX_OAUTH_ENABLED="true"
BITRIX_OAUTH_CLIENT_ID="..."
BITRIX_OAUTH_CLIENT_SECRET="..."
BITRIX_PORTAL_URL="https://example.bitrix24.ru"
BITRIX_TOKEN_DB_PATH="/data/tokens.db"
BITRIX_TOKEN_ENCRYPTION_KEY="..."   # Fernet key
BITRIX_ALLOWED_ACCESS="read,write"
MCP_BEARER_TOKEN="required-when-oauth-on"
MCP_PUBLIC_BASE_URL="https://bitrix-mcp.example.com"
```

Portal `member_id` is learned from the first successful OAuth response and stored in
SQLite — it is not an env variable.

Generate an encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

With OAuth on:

- Webhook writes are blocked unless `BITRIX_ALLOW_WEBHOOK_WRITES=true` (emergency only).
- `OwnershipGuard` allows mutating only records the user is responsible for
  (`BITRIX_OWNERSHIP_ADMIN_EMAILS` bypasses ownership, not OAuth).
- HTTP routes: `GET /oauth/callback`, `GET|POST /bitrix/app`, `GET /healthz`.

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

## Authentication

Shared bearer token for public deployments:

```dotenv
MCP_BEARER_TOKEN="change-this-long-random-token"
MCP_PUBLIC_BASE_URL="https://bitrix-mcp.example.com"
```

Clients send `Authorization: Bearer ...`. When `BITRIX_OAUTH_ENABLED=true`, bearer is **required**.

## Design notes

- Unknown / undocumented methods are denied by default (`BITRIX_ALLOW_UNKNOWN_METHODS` can open the valve).
- Deprecated methods are allowed with a warning.
- `batch` subcommands are checked individually; nested `batch` and write subcommands are denied.
- `BitrixIdentity` covers webhook + per-user OAuth; writes never use the webhook under OAuth mode.
