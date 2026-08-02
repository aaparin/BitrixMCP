from __future__ import annotations

import re
from typing import Any

import httpx
import logfire

from bitrix_mcp.errors import BitrixApiError, MethodNotAllowed, ReadOnlyViolation
from bitrix_mcp.identity import BitrixIdentity, coerce_identity
from bitrix_mcp.methods.catalog import MethodCatalog, default_catalog
from bitrix_mcp.methods.policy import MethodPolicy


# Re-export for back-compat with existing imports.
__all__ = [
    'BitrixApiError',
    'BitrixClient',
    'MethodNotAllowed',
    'ReadOnlyViolation',
    'normalize_webhook_url',
]


def normalize_webhook_url(webhook_url: str) -> str:
    return webhook_url.rstrip('/') + '/'


class BitrixClient:
    def __init__(
        self,
        identity: BitrixIdentity | str,
        *,
        timeout: float = 20,
        policy: MethodPolicy | None = None,
        catalog: MethodCatalog | None = None,
        allowed_access: frozenset[str] | None = None,
        allow_unknown_methods: bool = False,
        allow_webhook_writes: bool = False,
        read_only: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        ownership_guard: Any = None,
    ):
        self.identity = coerce_identity(identity)
        self.webhook_url = getattr(self.identity, 'base_url', None) or (
            normalize_webhook_url(identity) if isinstance(identity, str) else ''
        )

        self.timeout = timeout
        self.transport = transport
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self.ownership_guard = ownership_guard

        if policy is not None:
            self.policy = policy
        else:
            resolved_catalog = catalog or default_catalog()
            if allowed_access is None:
                if read_only is False:
                    allowed_access = frozenset({'read', 'write', 'destructive'})
                else:
                    allowed_access = frozenset({'read'})
            self.policy = MethodPolicy(
                catalog=resolved_catalog,
                allowed_access=frozenset(allowed_access),
                allow_unknown_methods=allow_unknown_methods,
                allow_webhook_writes=allow_webhook_writes,
            )

        # Back-compat flag used by older tests.
        self.read_only = 'write' not in self.policy.allowed_access and 'destructive' not in self.policy.allowed_access

    async def aclose(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self.timeout, transport=self.transport)
        return self._http_client

    async def call_method(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = await self.call_method_payload(method, params)
        return payload.get('result', payload) if isinstance(payload, dict) else payload

    async def call_method_payload(self, method: str, params: dict[str, Any] | None = None) -> Any:
        method = method.strip()
        if not method:
            raise BitrixApiError('Bitrix24 method name is empty.')

        params = self._normalize_params(method, params or {})
        decision = self.policy.decide(method, params, identity=self.identity)
        decision.raise_if_denied()

        if (
            self.ownership_guard is not None
            and decision.access in {'write', 'destructive'}
        ):
            ownership = await self.ownership_guard.check(self, method, params, self.identity)
            if not ownership.allowed:
                raise MethodNotAllowed(ownership.reason or 'Ownership check failed.')
            if ownership.mutated_params is not None:
                params = ownership.mutated_params

        auth_params = self.identity.auth_params()
        request_params = {**auth_params, **params} if auth_params else params
        url = self.identity.request_url(method)

        payload, status_code = await self._post_json(url, request_params, method=method)
        if self._is_auth_error(payload, status_code):
            refreshed = await self.identity.on_auth_error()
            if refreshed:
                auth_params = self.identity.auth_params()
                request_params = {**auth_params, **params} if auth_params else params
                url = self.identity.request_url(method)
                payload, status_code = await self._post_json(url, request_params, method=method)

        if status_code >= 400:
            message = (
                payload.get('error_description') or payload.get('error') or str(payload)
                if isinstance(payload, dict)
                else str(payload)
            )
            raise BitrixApiError(f'Bitrix24 HTTP {status_code}: {message}')

        if isinstance(payload, dict) and payload.get('error'):
            message = payload.get('error_description') or payload['error']
            raise BitrixApiError(f'Bitrix24 API error: {message}')

        if decision.warnings and isinstance(payload, dict):
            existing = payload.get('warnings')
            merged = list(existing) if isinstance(existing, list) else []
            merged.extend(decision.warnings)
            payload = {**payload, 'warnings': merged}

        return payload

    async def _post_json(self, url: str, params: dict[str, Any], *, method: str) -> tuple[Any, int]:
        client = self._get_http_client()
        with logfire.span('Bitrix24 REST request', bitrix_method=method):
            response = await client.post(url, json=params)

        try:
            payload = response.json()
        except ValueError as exc:
            raise BitrixApiError(f'Bitrix24 returned non-JSON response with HTTP {response.status_code}.') from exc

        return payload, response.status_code

    @staticmethod
    def _is_auth_error(payload: Any, status_code: int | None = None) -> bool:
        if status_code == 401:
            return True
        if not isinstance(payload, dict):
            return False
        error = str(payload.get('error') or '').lower()
        return error in {'expired_token', 'invalid_token', 'no_auth_found'}

    async def count_list_method(self, method: str, params: dict[str, Any] | None = None) -> int:
        request_params = dict(params or {})
        request_params.setdefault('select', ['ID'])
        request_params.setdefault('start', 0)
        payload = await self.call_method_payload(method, request_params)
        if isinstance(payload, dict) and isinstance(payload.get('total'), int):
            return payload['total']
        result = payload.get('result') if isinstance(payload, dict) else payload
        if isinstance(result, dict) and isinstance(result.get('total'), int):
            return result['total']
        if isinstance(result, list):
            return len(result)
        if isinstance(result, dict):
            for value in result.values():
                if isinstance(value, list):
                    return len(value)
        raise BitrixApiError(f'Bitrix24 did not return a countable list response for "{method}".')

    def _normalize_params(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        method = method.lower()
        if method.startswith('voximplant.'):
            params = self._normalize_voximplant_param_keys(params)
        else:
            params = self._normalize_common_param_keys(params)
        if method == 'crm.contact.list':
            self._ensure_select_fields(params, ['ID', 'NAME', 'LAST_NAME', 'ASSIGNED_BY_ID'])
        elif method == 'crm.company.list':
            self._normalize_company_list_params(params)
        elif method == 'user.get':
            self._normalize_user_get_params(params)
        return params

    def _normalize_voximplant_param_keys(self, params: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(params)
        for key in ['filter', 'sort', 'order']:
            upper_key = key.upper()
            if key in normalized and upper_key not in normalized:
                normalized[upper_key] = normalized.pop(key)
        return normalized

    def _normalize_common_param_keys(self, params: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(params)
        for key in ['filter', 'select', 'order', 'start']:
            upper_key = key.upper()
            if upper_key in normalized and key not in normalized:
                normalized[key] = normalized.pop(upper_key)
        return normalized

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

    def _normalize_user_get_params(self, params: dict[str, Any]) -> None:
        user_id = params.pop('USER_ID', None)
        if user_id is None:
            user_id = params.pop('user_id', None)
        if user_id is None:
            user_id = params.pop('ID', None)
        if user_id is None:
            user_id = params.pop('id', None)
        if user_id is not None and 'filter' not in params:
            params['filter'] = {'ID': int(user_id)}

        filter_ = params.get('filter')
        if isinstance(filter_, str):
            match = re.fullmatch(r'\s*ID\s*=\s*(\d+)\s*', filter_, flags=re.IGNORECASE)
            if match:
                params['filter'] = {'ID': int(match.group(1))}
        elif isinstance(filter_, dict):
            for key in ['=ID', 'id', '=id']:
                if key in filter_:
                    filter_['ID'] = filter_.pop(key)
                    break

        self._ensure_select_fields(params, ['ID', 'NAME', 'LAST_NAME', 'EMAIL'])
