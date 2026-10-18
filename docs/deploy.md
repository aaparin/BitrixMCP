# Deploy & configure BitrixMCP (production)

This guide covers a public HTTPS deployment with `docker-compose.server.yml`,
a Bitrix24 local application, and connecting the server to Claude Cowork or ChatGPT.

Example host used below: `https://mcpbitrix.dev.itcoll.com`

---

## How authorization works

```
MCP client ──(OAuth 2.1: DCR + PKCE)──► BitrixMCP (OAuth proxy) ──(OAuth 2.0)──► Bitrix24 portal
```

- BitrixMCP is the OAuth authorization server for MCP clients. It implements
  dynamic client registration, PKCE and RFC 8414 / RFC 9728 metadata, which
  Bitrix24 OAuth does not offer (FastMCP `OAuthProxy`).
- When a user adds the connector, the MCP client opens BitrixMCP's `/authorize`. After a
  consent page the user is sent to the portal's `/oauth/authorize/`, signs in to
  Bitrix24, and Bitrix24 redirects back to `/oauth/callback`.
- BitrixMCP exchanges the code at `oauth.bitrix.info`, stores the Bitrix24 tokens
  (encrypted) and gives the MCP client its own short-lived JWT + refresh token. Bitrix24
  token refresh is handled server-side.
- Every MCP request is validated with `user.current` under the user's Bitrix24
  token. **All tools, including method discovery (`methods` / `scope`), run as
  that user** with their portal permissions. No inbound webhook is needed.
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

## 2. Bitrix24 local application

1. In `https://bitrix24.dreamsite.lt`, open **Applications → Developer resources → Other → Local application** as an administrator.
2. Choose **Server** and register **with a UI** (leave “Uses API only” off). Leave **Initial installation path** empty and **Application completes the installation itself** off.
3. Set **Your handler path** to:

```text
https://mcpbitrix.dev.itcoll.com/oauth/callback
```

   Bitrix24 sends the OAuth redirect (GET) and the in-portal app/installation
   frames (POST) to this URL; BitrixMCP serves both.
4. Scopes: everything the tools need, e.g. CRM, tasks, users, departments and telephony. Bitrix24 still applies each signed-in user's own permissions.
5. Copy Application ID → `BITRIX_OAUTH_CLIENT_ID`, Application key → `BITRIX_OAUTH_CLIENT_SECRET`.

`BITRIX_PORTAL_URL` is the portal origin: `https://bitrix24.dreamsite.lt`.

---

## 3. Generate secrets

```bash
# Fernet key for encrypting stored OAuth state
docker compose -f docker-compose.server.yml run --rm bitrix-mcp \
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Optional: separate JWT signing key (otherwise derived from the client secret)
openssl rand -hex 32
```

---

## 4. Write `.env` next to `docker-compose.server.yml`

```dotenv
MCP_PUBLIC_BASE_URL="https://mcpbitrix.dev.itcoll.com"
BITRIX_ALLOWED_ACCESS="read,write"

BITRIX_OAUTH_ENABLED="true"
BITRIX_OAUTH_CLIENT_ID="from_local_application"
BITRIX_OAUTH_CLIENT_SECRET="from_local_application"
BITRIX_PORTAL_URL="https://bitrix24.dreamsite.lt"
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

## 5. Connect Claude

Claude Pro/Max: **Customize → Connectors → + Add → Add custom connector**. On Team/Enterprise, an owner first adds it under **Organization settings → Connectors → Add → Custom → Web**; members then connect under **Customize → Connectors**.

- **URL:** `https://mcpbitrix.dev.itcoll.com/mcp`
- Choose OAuth sign-in and **Register automatically** as the OAuth client. Leave request headers empty.

On **Connect** Claude opens the consent page, then the Bitrix24 sign-in; after
approving, the connector is ready. Each user connects with their own Bitrix24 account.
On Team / Enterprise plans an owner adds the connector once for the organization;
each member still signs in individually.

---

## 6. Connect ChatGPT

In a ChatGPT Business or Enterprise/Edu workspace, an admin or authorized developer opens **Plugins → + → Add custom MCP server**. Enter the same public URL `https://mcpbitrix.dev.itcoll.com/mcp` and choose OAuth authentication. Create the plugin, complete the Bitrix24 sign-in during the tool scan, then install it. An admin can publish it for the workspace after testing. Individual users authorize with their own Bitrix24 accounts.

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
