from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx
import logfire


READ_ONLY_ACTIONS = {
    'get',
    'list',
    'fields',
    'search',
    'count',
    'status',
    'history',
    'items',
    'types',
    'enum',
    'userfield',
}

WRITE_ACTIONS = {
    'add',
    'create',
    'set',
    'update',
    'delete',
    'remove',
    'bind',
    'unbind',
    'send',
    'import',
    'batch',
}


class BitrixApiError(RuntimeError):
    pass


class ReadOnlyViolation(BitrixApiError):
    pass


def normalize_webhook_url(webhook_url: str) -> str:
    return webhook_url.rstrip('/') + '/'


def is_read_only_method(method: str) -> bool:
    parts = [part.lower() for part in method.replace('-', '.').split('.') if part]
    if not parts:
        return False
    if any(part in WRITE_ACTIONS for part in parts):
        return False
    return any(part in READ_ONLY_ACTIONS for part in parts)


class BitrixClient:
    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = 20,
        read_only: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.webhook_url = normalize_webhook_url(webhook_url)
        self.timeout = timeout
        self.read_only = read_only
        self.transport = transport

    async def call_method(self, method: str, params: dict[str, Any] | None = None) -> Any:
        method = method.strip()
        if not method:
            raise BitrixApiError('Bitrix24 method name is empty.')
        if self.read_only and not is_read_only_method(method):
            raise ReadOnlyViolation(f'Method "{method}" is blocked by read-only mode.')

        params = self._normalize_params(method, params or {})
        url = urljoin(self.webhook_url, f'{method}.json')
        with logfire.span('Bitrix24 REST request', bitrix_method=method):
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(url, json=params)

        try:
            payload = response.json()
        except ValueError as exc:
            raise BitrixApiError(f'Bitrix24 returned non-JSON response with HTTP {response.status_code}.') from exc

        if response.status_code >= 400:
            message = payload.get('error_description') or payload.get('error') or response.text
            raise BitrixApiError(f'Bitrix24 HTTP {response.status_code}: {message}')

        if isinstance(payload, dict) and payload.get('error'):
            message = payload.get('error_description') or payload['error']
            raise BitrixApiError(f'Bitrix24 API error: {message}')

        return payload.get('result', payload) if isinstance(payload, dict) else payload

    def _normalize_params(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        method = method.lower()
        if method == 'crm.contact.list':
            self._ensure_select_fields(params, ['ID', 'NAME', 'LAST_NAME', 'ASSIGNED_BY_ID'])
        elif method == 'crm.company.list':
            self._normalize_company_list_params(params)
        return params

    def _ensure_select_fields(self, params: dict[str, Any], fields: list[str]) -> None:
        select = params.get('select')
        if isinstance(select, list):
            for field in fields:
                if field not in select:
                    select.append(field)
        elif 'select' not in params:
            params['select'] = fields.copy()

    def _normalize_company_list_params(self, params: dict[str, Any]) -> None:
        filter_ = params.get('filter')
        if isinstance(filter_, dict):
            name_value = None
            for key in ['=NAME', 'NAME', '%NAME']:
                if key in filter_:
                    name_value = filter_.pop(key)
                    break
            if name_value is not None:
                filter_['=TITLE'] = name_value

        select = params.get('select')
        if isinstance(select, list):
            params['select'] = ['TITLE' if field == 'NAME' else field for field in select]
        self._ensure_select_fields(params, ['ID', 'TITLE', 'ASSIGNED_BY_ID'])
