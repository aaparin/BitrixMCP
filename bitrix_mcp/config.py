from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True)
class Settings:
    bitrix_webhook_url: str
    docs_mcp_url: str
    openrouter_api_key: str
    openrouter_model: str
    local_llm_base_url: str
    local_llm_model: str
    local_llm_api_key: str
    host: str
    port: int
    docs_cache_ttl_seconds: int
    max_agent_steps: int
    request_timeout_seconds: float
    read_only: bool
    logfire_enabled: bool
    logfire_instrument_httpx: bool

    @classmethod
    def from_env(cls) -> Settings:
        load_env_file()
        return cls(
            bitrix_webhook_url=os.getenv('BITRIX_WEBHOOK_URL', '').strip(),
            docs_mcp_url=os.getenv('BITRIX_DOCS_MCP_URL', 'https://mcp-dev.bitrix24.com/mcp').strip(),
            openrouter_api_key=os.getenv('OPENROUTER_API_KEY', '').strip(),
            openrouter_model=os.getenv('OPENROUTER_MODEL', 'qwen/qwen3-32b').strip(),
            local_llm_base_url=os.getenv(
                'LOCAL_LLM_BASE_URL',
                os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434/v1'),
            ).strip(),
            local_llm_model=os.getenv('LOCAL_LLM_MODEL', os.getenv('OLLAMA_MODEL', 'qwen3:8b')).strip(),
            local_llm_api_key=os.getenv('LOCAL_LLM_API_KEY', 'api-key-not-set').strip(),
            host=os.getenv('MCP_HOST', '127.0.0.1').strip(),
            port=_env_int('MCP_PORT', 8000),
            docs_cache_ttl_seconds=_env_int('BITRIX_DOCS_CACHE_TTL_SECONDS', 3600),
            max_agent_steps=_env_int('BITRIX_AGENT_MAX_STEPS', 12),
            request_timeout_seconds=float(os.getenv('BITRIX_REQUEST_TIMEOUT_SECONDS', '20')),
            read_only=_env_bool('BITRIX_READ_ONLY', True),
            logfire_enabled=_env_bool('LOGFIRE_ENABLED', True),
            logfire_instrument_httpx=_env_bool('LOGFIRE_INSTRUMENT_HTTPX', False),
        )

    def validate_for_run(self) -> None:
        missing: list[str] = []
        if not self.bitrix_webhook_url:
            missing.append('BITRIX_WEBHOOK_URL')
        if not self.local_llm_base_url:
            missing.append('LOCAL_LLM_BASE_URL')
        if not self.local_llm_model:
            missing.append('LOCAL_LLM_MODEL')
        if not self.docs_mcp_url:
            missing.append('BITRIX_DOCS_MCP_URL')
        if missing:
            names = ', '.join(missing)
            raise RuntimeError(f'Missing required environment variables: {names}')
