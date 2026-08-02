# Deploy & configure BitrixMCP (production)

This guide covers a public HTTPS deployment with `docker-compose.server.yml`,
Bitrix24 webhook + local application OAuth, and LibreChat.

Example host used below: `https://mcpbitrix.dev.itcoll.com`

---

## 1. Public URLs the reverse proxy must expose

| URL | Purpose |
|---|---|
| `https://mcpbitrix.dev.itcoll.com/mcp` | MCP endpoint (LibreChat) |
| `https://mcpbitrix.dev.itcoll.com/oauth/callback` | OAuth redirect from Bitrix24 |
| `https://mcpbitrix.dev.itcoll.com/bitrix/app` | Fallback in-portal app page |
| `https://mcpbitrix.dev.itcoll.com/healthz` | Liveness check |

`MCP_PUBLIC_BASE_URL` must be the **origin only**:

```dotenv
MCP_PUBLIC_BASE_URL="https://mcpbitrix.dev.itcoll.com"
```

Do **not** append `/mcp`. The OAuth callback is `/oauth/callback` on the host root,
not under `/mcp`.

Proxy `/mcp`, `/oauth/`, `/bitrix/`, and `/healthz` to the container (port 8000).

Smoke check:

```bash
curl -sS https://mcpbitrix.dev.itcoll.com/healthz
# expected: ok
```

---

## 2. Bitrix24 inbound webhook (reads)

1. Bitrix24 → **Developer resources** → **Other** → **Inbound webhook**.
2. Grant scopes needed for CRM, tasks, users, etc.
3. Copy the URL:

```text
https://<portal>.bitrix24.ru/rest/<user_id>/<token>/
```

Store it as `BITRIX_WEBHOOK_URL`.

---

## 3. Bitrix24 local application (per-user writes)

1. **Applications → Developer resources → Other → Local application**.
2. Do **not** enable “Application uses only API” — register **with a UI**
   (needed for `/bitrix/app`).
3. Handler / redirect URI:

```text
https://mcpbitrix.dev.itcoll.com/oauth/callback
```

4. Optional application URL (placement / self-service):

```text
https://mcpbitrix.dev.itcoll.com/bitrix/app
```

5. Scopes must cover at least what the webhook can do, for example:
   `crm, task, tasks, user, department, calendar, im, …`
6. Copy:
   - Application ID → `BITRIX_OAUTH_CLIENT_ID`
   - Application key → `BITRIX_OAUTH_CLIENT_SECRET`

`BITRIX_PORTAL_URL` is the portal origin, e.g. `https://company.bitrix24.ru`.

Portal `member_id` is **not** configured in env. It is returned by Bitrix24 on the
first successful OAuth (or placement) and pinned in SQLite (`portal_settings`).
Later callbacks from a different portal are rejected.

---

## 4. Generate secrets on the server

In the compose project directory:

```bash
# MCP bearer token
openssl rand -hex 32

# Fernet key for encrypting stored OAuth tokens
docker compose -f docker-compose.server.yml run --rm bitrix-mcp \
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 5. Write `.env` next to `docker-compose.server.yml`

```dotenv
# --- Bitrix read (webhook) ---
BITRIX_WEBHOOK_URL="https://YOUR_PORTAL.bitrix24.ru/rest/1/XXXXXXXX/"

# --- MCP ---
MCP_BEARER_TOKEN="paste_openssl_rand_hex_32"
MCP_PUBLIC_BASE_URL="https://mcpbitrix.dev.itcoll.com"

# Writes require write in the access policy
BITRIX_ALLOWED_ACCESS="read,write"

# --- OAuth (per-user writes) ---
BITRIX_OAUTH_ENABLED="true"
BITRIX_OAUTH_CLIENT_ID="from_local_application"
BITRIX_OAUTH_CLIENT_SECRET="from_local_application"
BITRIX_PORTAL_URL="https://YOUR_PORTAL.bitrix24.ru"
BITRIX_TOKEN_ENCRYPTION_KEY="fernet_key_from_step_4"
# Token DB path is set in compose to /data/tokens.db (named volume)

# Optional
# BITRIX_USER_EMAIL_HEADER="X-Bitrix-User-Email"
# BITRIX_OWNERSHIP_ADMIN_EMAILS="admin@company.ru"
# BITRIX_AUTH_WAIT_SECONDS="120"
```

| Secret | Where |
|---|---|
| Webhook URL | `.env` → `BITRIX_WEBHOOK_URL` |
| App client_id / client_secret | `.env` → `BITRIX_OAUTH_CLIENT_*` |
| Bearer | `.env` → `MCP_BEARER_TOKEN` **and** LibreChat MCP config |
| Fernet key | `.env` → `BITRIX_TOKEN_ENCRYPTION_KEY` (server only) |

Restart:

```bash
docker compose -f docker-compose.server.yml up -d --build
docker logs bitrix-mcp --tail 50
```

With OAuth on you should see `OAuth enabled: …`. Missing required vars abort startup
with `Missing or invalid configuration: …`.

---

## 6. LibreChat

- **URL:** `https://mcpbitrix.dev.itcoll.com/mcp`
- **Authorization:** `Bearer <same MCP_BEARER_TOKEN>`
- User identity header (required for writes):

```http
X-Bitrix-User-Email: user@company.ru
```

LDAP login must match the Bitrix24 user email. Without the header, reads use the
webhook; writes return `identity_missing` / ask for authorization.

---

## 7. End-to-end check

1. `curl https://mcpbitrix.dev.itcoll.com/healthz` → `ok`
2. In chat: `bitrix_whoami` → email / mode
3. Any write tool or `bitrix_authorize` → authorization link
4. Open the link → redirect to `/oauth/callback` → **Connected**
5. Pending write continues (or retry works immediately)
6. Prefer `dry_run=true` first, then `dry_run=false`

---

## 8. Common failures

| Symptom | Likely cause |
|---|---|
| Callback 404 | Proxy only forwards `/mcp`; open `/oauth/*` |
| redirect_uri mismatch | App redirect must be exactly `…/oauth/callback` |
| Startup fails with bearer missing | `MCP_BEARER_TOKEN` required when OAuth is on |
| Email mismatch page | Browser Bitrix session ≠ LibreChat email |
| Write `forbidden` | `BITRIX_ALLOWED_ACCESS` still `read` only |
| Tokens unreadable after restart | Lost / changed `BITRIX_TOKEN_ENCRYPTION_KEY` — users re-authorize |

---

## 9. Compose notes

`docker-compose.server.yml`:

- Loads `.env`
- Forces `MCP_TRANSPORT=streamable-http`, `MCP_PATH=/mcp`
- Persists OAuth SQLite at `/data/tokens.db` via volume `bitrix-mcp-data`
- Joins external network `librechat_default` (adjust if your stack differs)
