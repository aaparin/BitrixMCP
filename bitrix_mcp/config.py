from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path

from bitrix_mcp.oauth.store import TokenStore


VALID_ACCESS_LEVELS = frozenset({'read', 'write', 'destructive', 'unknown'})


def load_env_file(path: str | Path = '.env', *, override: bool = False) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line.removeprefix('export ').strip()
        if '=' not in line:
            continue

        name, value = line.split('=', 1)
        name = name.strip()
        if not name:
            continue
        if not override and name in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[name] = value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _parse_allowed_access(raw: str | None) -> frozenset[str]:
    if raw is None or not raw.strip():
        return frozenset({'read'})
    levels = {part.strip().lower() for part in raw.split(',') if part.strip()}
    invalid = levels - VALID_ACCESS_LEVELS
    if invalid:
        names = ', '.join(sorted(invalid))
        raise RuntimeError(
            f'Invalid BITRIX_ALLOWED_ACCESS value(s): {names}. '
            f'Allowed: {", ".join(sorted(VALID_ACCESS_LEVELS))}.'
        )
    return frozenset(levels)


def _resolve_allowed_access() -> frozenset[str]:
    raw = os.getenv('BITRIX_ALLOWED_ACCESS')
    if raw is not None and raw.strip():
        return _parse_allowed_access(raw)

    legacy = os.getenv('BITRIX_READ_ONLY')
    if legacy is not None:
        warnings.warn(
            'BITRIX_READ_ONLY is deprecated; use BITRIX_ALLOWED_ACCESS instead '
            '(e.g. "read" or "read,write,destructive").',
            DeprecationWarning,
            stacklevel=2,
        )
        if legacy.strip().lower() in {'1', 'true', 'yes', 'on'}:
            return frozenset({'read'})
        return frozenset({'read', 'write', 'destructive'})

    return frozenset({'read'})


def _parse_email_list(raw: str | None) -> frozenset[str]:
    if not raw or not raw.strip():
        return frozenset()
    return frozenset(TokenStore.normalize_email(part) for part in raw.split(',') if part.strip())


def _parse_csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return default
    values = tuple(part.strip() for part in raw.split(',') if part.strip())
    return values or default


@dataclass(frozen=True)
class Settings:
    bitrix_webhook_url: str
    host: str
    port: int
    transport: str
    path: str
    bearer_token: str
    public_base_url: str
    request_timeout_seconds: float
    allowed_access: frozenset[str]
    allow_unknown_methods: bool
    method_catalog_path: str
    portal_methods_ttl_seconds: int
    crm_metadata_ttl_seconds: int
    crm_statuses_ttl_seconds: int
    logfire_enabled: bool
    logfire_instrument_httpx: bool
    # OAuth / per-user writes
    oauth_enabled: bool
    oauth_client_id: str
    oauth_client_secret: str
    portal_url: str
    member_id: str
    user_email_header: str
    token_db_path: str
    token_encryption_key: str
    allow_webhook_writes: bool
    write_ownership: str
    ownership_admin_emails: frozenset[str]
    task_ownership_fields: tuple[str, ...]
    auth_state_ttl_seconds: int
    auth_wait_seconds: int
    token_refresh_skew_seconds: int

    @classmethod
    def from_env(cls) -> Settings:
        load_env_file()
        transport = os.getenv('MCP_TRANSPORT', 'sse').strip().lower()
        default_path = '/sse' if transport == 'sse' else '/mcp'
        return cls(
            bitrix_webhook_url=os.getenv('BITRIX_WEBHOOK_URL', '').strip(),
            host=os.getenv('MCP_HOST', '127.0.0.1').strip(),
            port=_env_int('MCP_PORT', 8000),
            transport=transport,
            path=os.getenv('MCP_PATH', default_path).strip(),
            bearer_token=os.getenv('MCP_BEARER_TOKEN', '').strip(),
            public_base_url=os.getenv('MCP_PUBLIC_BASE_URL', '').strip(),
            request_timeout_seconds=float(os.getenv('BITRIX_REQUEST_TIMEOUT_SECONDS', '20')),
            allowed_access=_resolve_allowed_access(),
            allow_unknown_methods=_env_bool('BITRIX_ALLOW_UNKNOWN_METHODS', False),
            method_catalog_path=os.getenv('BITRIX_METHOD_CATALOG_PATH', '').strip(),
            portal_methods_ttl_seconds=_env_int('BITRIX_PORTAL_METHODS_TTL_SECONDS', 3600),
            crm_metadata_ttl_seconds=_env_int('BITRIX_CRM_METADATA_TTL_SECONDS', 604800),
            crm_statuses_ttl_seconds=_env_int('BITRIX_CRM_STATUSES_TTL_SECONDS', 900),
            logfire_enabled=_env_bool('LOGFIRE_ENABLED', True),
            logfire_instrument_httpx=_env_bool('LOGFIRE_INSTRUMENT_HTTPX', False),
            oauth_enabled=_env_bool('BITRIX_OAUTH_ENABLED', False),
            oauth_client_id=os.getenv('BITRIX_OAUTH_CLIENT_ID', '').strip(),
            oauth_client_secret=os.getenv('BITRIX_OAUTH_CLIENT_SECRET', '').strip(),
            portal_url=os.getenv('BITRIX_PORTAL_URL', '').strip(),
            member_id=os.getenv('BITRIX_MEMBER_ID', '').strip(),
            user_email_header=os.getenv('BITRIX_USER_EMAIL_HEADER', 'X-Bitrix-User-Email').strip()
            or 'X-Bitrix-User-Email',
            token_db_path=os.getenv('BITRIX_TOKEN_DB_PATH', '/data/tokens.db').strip() or '/data/tokens.db',
            token_encryption_key=os.getenv('BITRIX_TOKEN_ENCRYPTION_KEY', '').strip(),
            allow_webhook_writes=_env_bool('BITRIX_ALLOW_WEBHOOK_WRITES', False),
            write_ownership=os.getenv('BITRIX_WRITE_OWNERSHIP', 'strict').strip().lower() or 'strict',
            ownership_admin_emails=_parse_email_list(os.getenv('BITRIX_OWNERSHIP_ADMIN_EMAILS')),
            task_ownership_fields=_parse_csv(
                os.getenv('BITRIX_TASK_OWNERSHIP_FIELDS'),
                ('RESPONSIBLE_ID',),
            ),
            auth_state_ttl_seconds=_env_int('BITRIX_AUTH_STATE_TTL_SECONDS', 900),
            auth_wait_seconds=_env_int('BITRIX_AUTH_WAIT_SECONDS', 120),
            token_refresh_skew_seconds=_env_int('BITRIX_TOKEN_REFRESH_SKEW_SECONDS', 300),
        )

    @property
    def read_only(self) -> bool:
        return self.allowed_access == frozenset({'read'})

    @property
    def ownership_enabled(self) -> bool:
        return self.write_ownership != 'off'

    def validate_for_run(self) -> None:
        missing: list[str] = []
        if not self.bitrix_webhook_url:
            missing.append('BITRIX_WEBHOOK_URL')
        if self.transport not in {'sse', 'http', 'streamable-http'}:
            missing.append('MCP_TRANSPORT must be one of: sse, http, streamable-http')
        if not self.path.startswith('/'):
            missing.append('MCP_PATH must start with /')
        if self.public_base_url and not self.public_base_url.startswith(('http://', 'https://')):
            missing.append('MCP_PUBLIC_BASE_URL must start with http:// or https://')
        if not self.allowed_access:
            missing.append('BITRIX_ALLOWED_ACCESS must not be empty')
        if self.write_ownership not in {'strict', 'off'}:
            missing.append('BITRIX_WRITE_OWNERSHIP must be strict or off')

        if self.oauth_enabled:
            if not self.oauth_client_id:
                missing.append('BITRIX_OAUTH_CLIENT_ID')
            if not self.oauth_client_secret:
                missing.append('BITRIX_OAUTH_CLIENT_SECRET')
            if not self.portal_url:
                missing.append('BITRIX_PORTAL_URL')
            elif not self.portal_url.startswith(('http://', 'https://')):
                missing.append('BITRIX_PORTAL_URL must start with http:// or https://')
            if not self.member_id:
                missing.append('BITRIX_MEMBER_ID')
            if not self.token_encryption_key:
                missing.append('BITRIX_TOKEN_ENCRYPTION_KEY')
            if not self.public_base_url:
                missing.append('MCP_PUBLIC_BASE_URL')
            if not self.bearer_token:
                missing.append('MCP_BEARER_TOKEN (required when BITRIX_OAUTH_ENABLED=true)')

        if missing:
            names = ', '.join(missing)
            raise RuntimeError(f'Missing or invalid configuration: {names}')
