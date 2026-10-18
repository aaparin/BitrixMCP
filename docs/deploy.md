# Deploy & configure BitrixMCP (production)

This guide covers a public HTTPS deployment with `docker-compose.server.yml`,
a Bitrix24 local application, and connecting the server to Claude
(Cowork / claude.ai / Claude Desktop) as a custom connector.

Example host used below: `https://mcpbitrix.dev.itcoll.com`

---

## How authorization works

```
Claude ──(OAuth 2.1: DCR + PKCE)──► BitrixMCP (OAuth proxy) ──(OAuth 2.0)──► Bitrix24 portal
```

- BitrixMCP is the OAuth authorization server for MCP clients. It implements
  dynamic client registration, PKCE and RFC 8414 / RFC 9728 metadata, which
  Bitrix24 OAuth does not offer (FastMCP `OAuthProxy`).
- When a user adds the connector, Claude opens BitrixMCP's `/authorize`. After a
  consent page the user is sent to the portal's `/oauth/authorize/`, signs in to
  Bitrix24, and Bitrix24 redirects back to `/oauth/callback`.
- BitrixMCP exchanges the code at `oauth.bitrix.info`, stores the Bitrix24 tokens
  (encrypted) and gives Claude its own short-lived JWT + refresh token. Bitrix24
  token refresh is handled server-side.
- Every MCP request is validated with `user.current` under the user's Bitrix24
  token. **All tools — reads and writes — run as that user**, with their portal
  permissions. The inbound webhook is used only for method discovery
  (`methods` / `scope`).
- The portal `member_id` is pinned on the first successful sign-in; tokens from
  another portal are rejected.

---

## 1. Public URLs the reverse proxy must expose

| URL | Purpose |
|---|---|
| `/mcp` | MCP endpoint (Streamable HTTP) |
| `/.well-known/oauth-protected-resource/mcp` | Resource metadata (RFC 9728) |
| `/.well-known/oauth-authorization-server` | Authorization server metadata (RFC 8414) |
| `/register` | Dynamic client registration |
| `/authorize`, `/consent`, `/token` | OAuth endpoints for MCP clients |
| `/oauth/callback` | GET: redirect from Bitrix24. POST: in-portal app page |
| `/bitrix/app` | In-portal app page |
| `/healthz` | Liveness check |

Simplest: proxy the whole host to the container (port 8000). Do not buffer
`/mcp` responses (Streamable HTTP may use SSE).

`MCP_PUBLIC_BASE_URL` must be the **origin only**:

```dotenv
MCP_PUBLIC_BASE_URL="https://mcpbitrix.dev.itcoll.com"
```

Smoke checks:

```bash
curl -sS https://mcpbitrix.dev.itcoll.com/healthz
curl -sS https://mcpbitrix.dev.itcoll.com/.well-known/oauth-protected-resource/mcp
```

---

## 2. Bitrix24 inbound webhook (method discovery)

1. Bitrix24 → **Developer resources** → **Other** → **Inbound webhook**.
2. Grant the same scopes as the application.
3. Store the URL as `BITRIX_WEBHOOK_URL`.

---

## 3. Bitrix24 local application

1. **Applications → Developer resources → Other → Local application**.
2. Register **with a UI** (do not enable “Application uses only API”).
3. Handler path:

```text
https://mcpbitrix.dev.itcoll.com/oauth/callback
```

   Bitrix24 sends the OAuth redirect (GET) and the in-portal app/installation
   frames (POST) to this URL; BitrixMCP serves both.
4. Scopes: everything the tools need, e.g. `crm, task, tasks, user, department, telephony, …`.
5. Copy Application ID → `BITRIX_OAUTH_CLIENT_ID`, Application key → `BITRIX_OAUTH_CLIENT_SECRET`.

`BITRIX_PORTAL_URL` is the portal origin, e.g. `https://company.bitrix24.ru`.

---

## 4. Generate secrets

```bash
# Fernet key for encrypting stored OAuth state
docker compose -f docker-compose.server.yml run --rm bitrix-mcp \
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Optional: separate JWT signing key (otherwise derived from the client secret)
openssl rand -hex 32
```

---

## 5. Write `.env` next to `docker-compose.server.yml`

```dotenv
BITRIX_WEBHOOK_URL="https://YOUR_PORTAL.bitrix24.ru/rest/1/XXXXXXXX/"
MCP_PUBLIC_BASE_URL="https://mcpbitrix.dev.itcoll.com"
BITRIX_ALLOWED_ACCESS="read,write"

BITRIX_OAUTH_ENABLED="true"
BITRIX_OAUTH_CLIENT_ID="from_local_application"
BITRIX_OAUTH_CLIENT_SECRET="from_local_application"
BITRIX_PORTAL_URL="https://YOUR_PORTAL.bitrix24.ru"
BITRIX_TOKEN_ENCRYPTION_KEY="fernet_key_from_step_4"

# Optional
# MCP_JWT_SIGNING_KEY="openssl_rand_hex_32"
# MCP_ALLOWED_CLIENT_REDIRECT_URIS="https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback"
# BITRIX_OWNERSHIP_ADMIN_EMAILS="admin@company.ru"
```

State lives on the `/data` volume:

- `/data/tokens.db` — pinned portal + write audit log
- `/data/oauth-proxy/` — client registrations and Bitrix24 tokens (Fernet-encrypted)

Keep the volume, `BITRIX_TOKEN_ENCRYPTION_KEY` and the JWT key stable — losing
any of them signs every user out (they reconnect the connector).

```bash
docker compose -f docker-compose.server.yml up -d --build
docker logs bitrix-mcp --tail 50
```

You should see `OAuth enabled: …`.

---

## 6. Connect Claude

Claude (Cowork / claude.ai / Desktop) → **Settings → Connectors → Add custom connector**:

- **URL:** `https://mcpbitrix.dev.itcoll.com/mcp`
- Leave OAuth client ID / secret empty — Claude registers itself via DCR.

On **Connect** Claude opens the consent page, then the Bitrix24 sign-in; after
approving, the connector is ready. Each user connects with their own Bitrix24 account.
On Team / Enterprise plans an owner adds the connector once for the organization;
each member still signs in individually.

---

## 7. End-to-end check

1. `bitrix_whoami` → your Bitrix24 user, `mode: oauth`
2. A read tool (e.g. `crm_items_list`) returns only what you can see in Bitrix24
3. A write tool with `dry_run=true`, then `dry_run=false`

---

## 8. Common failures

| Symptom | Likely cause |
|---|---|
| Connector says it can't reach / authorize | `/.well-known/*`, `/register`, `/authorize`, `/token` not proxied |
| Bitrix24 shows redirect_uri error | Application handler is not on the `MCP_PUBLIC_BASE_URL` host |
| “Token exchange … failed” page | Wrong client secret, or portal `member_id` differs from the pinned one |
| Write `forbidden` | `BITRIX_ALLOWED_ACCESS` still `read` only |
| Everyone signed out after redeploy | `/data` volume, encryption key, or JWT key changed |
