from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import logfire

from bitrix_mcp.bitrix import BitrixClient
from bitrix_mcp.cache import TTLCache
from bitrix_mcp.methods.catalog import normalize_method_name


PORTAL_METHODS_CACHE = TTLCache(ttl_seconds=3600)


@dataclass
class PortalMethods:
    granted_methods: set[str] = field(default_factory=set)
    all_methods: set[str] | None = None
    granted_scopes: set[str] = field(default_factory=set)
    all_scopes: set[str] | None = None
    error: str | None = None

    def is_available(self, method: str) -> bool | None:
        if self.error and not self.granted_methods:
            return None
        normalized = normalize_method_name(method)
        granted = {normalize_method_name(item) for item in self.granted_methods}
        return normalized in granted

    def to_public_dict(self) -> dict[str, Any]:
        return {
            'grantedMethodCount': len(self.granted_methods),
            'allMethodCount': len(self.all_methods) if self.all_methods is not None else None,
            'grantedScopes': sorted(self.granted_scopes),
            'allScopeCount': len(self.all_scopes) if self.all_scopes is not None else None,
            'error': self.error,
        }


class AvailabilityProvider:
    def __init__(
        self,
        bitrix: BitrixClient,
        *,
        cache: TTLCache | None = None,
        ttl_seconds: int | None = None,
    ):
        self.bitrix = bitrix
        self.cache = cache or PORTAL_METHODS_CACHE
        if ttl_seconds is not None:
            self.cache.ttl_seconds = ttl_seconds

    async def get(self) -> PortalMethods:
        scope = getattr(self.bitrix.identity, 'cache_key', '')
        cache_key = self.cache.make_key('portal_methods', {}, scope=scope)
        cached = self.cache.get(cache_key)
        if isinstance(cached, PortalMethods):
            return cached

        portal = await self._fetch()
        self.cache.set(cache_key, portal)
        return portal

    async def _fetch(self) -> PortalMethods:
        portal = PortalMethods()
        try:
            with logfire.span('Load Bitrix portal methods'):
                granted = await self.bitrix.call_method('methods', {})
                portal.granted_methods = _as_method_set(granted)

                try:
                    full = await self.bitrix.call_method('methods', {'full': True})
                    portal.all_methods = _as_method_set(full)
                except Exception as exc:  # pragma: no cover - optional enrichment
                    portal.error = f'methods(full) failed: {exc}'

                scopes = await self.bitrix.call_method('scope', {})
                portal.granted_scopes = _as_string_set(scopes)

                try:
                    all_scopes = await self.bitrix.call_method('scope', {'full': True})
                    portal.all_scopes = _as_string_set(all_scopes)
                except Exception as exc:  # pragma: no cover - optional enrichment
                    detail = f'scope(full) failed: {exc}'
                    portal.error = f'{portal.error}; {detail}' if portal.error else detail
        except Exception as exc:
            portal.error = str(exc)
            logfire.warn('Failed to load Bitrix portal methods', error=str(exc))
        return portal


def _as_method_set(value: Any) -> set[str]:
    if isinstance(value, dict):
        # Some portals return {"method.name": true, ...}
        if all(isinstance(key, str) for key in value):
            if all(isinstance(item, bool) for item in value.values()):
                return {key for key, enabled in value.items() if enabled}
            return set(value.keys())
    if isinstance(value, list):
        return {str(item) for item in value if isinstance(item, str)}
    return set()


def _as_string_set(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item) for item in value if item is not None}
    if isinstance(value, dict):
        return {str(key) for key in value}
    return set()
