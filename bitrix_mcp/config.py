from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path


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
        )

    @property
    def read_only(self) -> bool:
        """Compatibility helper: true when only read access is allowed."""
        return self.allowed_access == frozenset({'read'})

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
        if missing:
            names = ', '.join(missing)
            raise RuntimeError(f'Missing or invalid configuration: {names}')
