from __future__ import annotations

from typing import Any

import logfire

from bitrix_mcp.bitrix import BitrixApiError, BitrixClient
from bitrix_mcp.cache import TTLCache
from bitrix_mcp.direct_tools import paginate_bitrix_list


CRM_STATUSES_CACHE = TTLCache(ttl_seconds=15 * 60)
_ALL_ENTITY_ID = '*'


def _identity_scope(bitrix: BitrixClient) -> str:
    return getattr(getattr(bitrix, 'identity', None), 'cache_key', '') or ''


def _cache_key(cache: TTLCache, bitrix: BitrixClient, entity_id: str | None) -> str:
    return cache.make_key(
        'crm_statuses',
        {'entity_id': (entity_id or _ALL_ENTITY_ID).upper()},
        scope=_identity_scope(bitrix),
    )


def invalidate_crm_statuses(
    bitrix: BitrixClient,
    entity_id: str | None = None,
    *,
    cache: TTLCache | None = None,
) -> int:
    """Drop cached statuses for a portal. Always clears the full-index entry too."""
    cache = cache or CRM_STATUSES_CACHE
    scope = _identity_scope(bitrix)
    if entity_id:
        removed = int(cache.delete(_cache_key(cache, bitrix, entity_id)))
        removed += int(cache.delete(_cache_key(cache, bitrix, None)))
        return removed
    prefix = f'{scope}:crm_statuses:' if scope else 'crm_statuses:'
    return cache.invalidate_prefix(prefix)


def compact_status(item: dict[str, Any]) -> dict[str, Any]:
    extra = item.get('EXTRA') if isinstance(item.get('EXTRA'), dict) else {}
    semantics = item.get('SEMANTICS') or extra.get('SEMANTICS')
    compact = {
        'id': item.get('ID'),
        'statusId': item.get('STATUS_ID'),
        'entityId': item.get('ENTITY_ID'),
        'name': item.get('NAME'),
        'nameInit': item.get('NAME_INIT'),
        'sort': _maybe_int(item.get('SORT')),
        'system': item.get('SYSTEM'),
        'semantics': semantics,
        'color': item.get('COLOR') or extra.get('COLOR'),
        'categoryId': item.get('CATEGORY_ID'),
    }
    return {key: value for key, value in compact.items() if value not in (None, '')}


def _maybe_int(value: Any) -> int | Any:
    if value is None or isinstance(value, bool):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _normalize_entity_id(entity_id: str | None) -> str | None:
    if entity_id is None:
        return None
    normalized = str(entity_id).strip()
    if not normalized:
        raise BitrixApiError('entity_id must be a non-empty string when provided.')
    return normalized.upper()


async def fetch_crm_statuses_raw(
    bitrix: BitrixClient,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    """Paginate crm.status.list. Docs cap a page at 50 items."""
    params: dict[str, Any] = {'order': {'SORT': 'ASC'}, 'start': 0}
    if entity_id:
        params['filter'] = {'ENTITY_ID': entity_id}
    return await paginate_bitrix_list(bitrix, 'crm.status.list', params)


async def get_crm_statuses_raw(
    bitrix: BitrixClient,
    entity_id: str | None = None,
    *,
    cache: TTLCache | None = None,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Return raw Bitrix status rows and whether they came from cache."""
    cache = cache or CRM_STATUSES_CACHE
    entity_id = _normalize_entity_id(entity_id)
    cache_key = _cache_key(cache, bitrix, entity_id)

    cached = None if force_refresh else cache.get(cache_key)
    if isinstance(cached, list):
        logfire.info('Bitrix CRM statuses cache hit', entity_id=entity_id or _ALL_ENTITY_ID)
        return cached, True

    with logfire.span('Load Bitrix CRM statuses', entity_id=entity_id or _ALL_ENTITY_ID):
        raw = await fetch_crm_statuses_raw(bitrix, entity_id)
    cache.set(cache_key, raw)
    if entity_id is None:
        _seed_entity_caches(cache, bitrix, raw)
    return raw, False


async def load_crm_statuses(
    bitrix: BitrixClient,
    entity_id: str | None = None,
    *,
    cache: TTLCache | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Shared loader for MCP resource + crm_statuses_list tool."""
    normalized = _normalize_entity_id(entity_id)
    raw, from_cache = await get_crm_statuses_raw(
        bitrix,
        normalized,
        cache=cache,
        force_refresh=force_refresh,
    )
    return format_crm_statuses(raw, entity_id=normalized, cached=from_cache)


def _seed_entity_caches(cache: TTLCache, bitrix: BitrixClient, raw: list[dict[str, Any]]) -> None:
    by_entity: dict[str, list[dict[str, Any]]] = {}
    for item in raw:
        eid = str(item.get('ENTITY_ID') or '').upper()
        if not eid:
            continue
        by_entity.setdefault(eid, []).append(item)
    for eid, items in by_entity.items():
        cache.set(_cache_key(cache, bitrix, eid), items)


def format_crm_statuses(
    raw: list[dict[str, Any]],
    *,
    entity_id: str | None = None,
    cached: bool = False,
) -> dict[str, Any]:
    statuses = [compact_status(item) for item in raw if isinstance(item, dict)]
    if entity_id:
        statuses = [item for item in statuses if str(item.get('entityId') or '').upper() == entity_id]
        return {
            'entityId': entity_id,
            'count': len(statuses),
            'statuses': statuses,
            'cached': cached,
        }

    by_entity: dict[str, list[dict[str, Any]]] = {}
    for item in statuses:
        eid = str(item.get('entityId') or '')
        if not eid:
            continue
        by_entity.setdefault(eid, []).append(item)

    return {
        'entityId': None,
        'entityIds': sorted(by_entity),
        'count': len(statuses),
        'byEntityId': {key: by_entity[key] for key in sorted(by_entity)},
        'cached': cached,
    }


async def crm_statuses_list_data(
    bitrix: BitrixClient,
    entity_id: str | None = None,
    *,
    cache: TTLCache | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    return await load_crm_statuses(
        bitrix,
        entity_id,
        cache=cache,
        force_refresh=force_refresh,
    )
