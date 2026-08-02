from __future__ import annotations

from typing import Any

from bitrix_mcp.bitrix import BitrixApiError, BitrixClient
from bitrix_mcp.records import (
    compact_records,
    extract_records,
    normalize_text,
    user_display_name,
)
from bitrix_mcp.read_tools import normalize_task_status_filter


def user_matches_query(user: dict[str, Any], query: str) -> bool:
    normalized_query = normalize_text(query)
    values = [
        user.get('NAME'),
        user.get('LAST_NAME'),
        user.get('EMAIL'),
        user_display_name(user),
    ]
    normalized_values = [normalize_text(str(value)) for value in values if value]
    return any(normalized_query in value or value in normalized_query for value in normalized_values)


def build_user_search_filters(query: str) -> list[dict[str, Any]]:
    parts = [part for part in query.strip().split() if part]
    filters: list[dict[str, Any]] = []
    if len(parts) >= 2:
        first, last = parts[0], ' '.join(parts[1:])
        filters.append({'NAME': first, 'LAST_NAME': last})
        filters.append({'NAME': last, 'LAST_NAME': first})
    if parts:
        filters.append({'NAME': parts[0]})
        filters.append({'LAST_NAME': parts[-1]})
    if '@' in query:
        filters.append({'EMAIL': query})
    return filters or [{'NAME': query}]


def dedupe_users(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for user in users:
        user_id = str(user.get('ID') or '')
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        deduped.append(user)
    return deduped


async def employees_search_data(
    bitrix: BitrixClient,
    query: str,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise BitrixApiError('query must be a non-empty string.')
    limit = max(1, min(int(limit), 20))

    users: list[dict[str, Any]] = []
    for filter_ in build_user_search_filters(query):
        result = await bitrix.call_method(
            'user.get',
            {'filter': filter_, 'select': ['ID', 'NAME', 'LAST_NAME', 'EMAIL', 'ACTIVE']},
        )
        records = extract_records(result) or []
        users.extend(user for user in records if user_matches_query(user, query))

    users = dedupe_users(users)
    return {
        'query': query,
        'users': compact_records(users, limit=limit),
        'totalMatched': len(users),
    }


async def tasks_list_for_employee_data(
    bitrix: BitrixClient,
    user_name: str,
    *,
    status: str | int | None = 'open',
    limit: int = 10,
) -> dict[str, Any]:
    user_name = user_name.strip()
    if not user_name:
        raise BitrixApiError('user_name must be a non-empty string.')
    limit = max(1, min(int(limit), 20))

    search = await employees_search_data(bitrix, user_name, limit=5)
    users = extract_records(search.get('users')) or []
    if not users:
        return {'found': False, 'userName': user_name, 'tasks': []}
    if len(users) > 1:
        return {
            'found': False,
            'ambiguous': True,
            'userName': user_name,
            'candidates': users,
        }

    user = users[0]
    filter_: dict[str, Any] = {'RESPONSIBLE_ID': user['ID']}
    filter_.update(normalize_task_status_filter(status))
    tasks = await bitrix.call_method(
        'tasks.task.list',
        {
            'filter': filter_,
            'select': ['ID', 'TITLE', 'STATUS', 'RESPONSIBLE_ID', 'CREATED_DATE', 'DEADLINE'],
            'start': 0,
        },
    )
    return {
        'found': True,
        'user': user,
        'tasks': compact_records(tasks, limit=limit),
    }


__all__ = [
    'build_user_search_filters',
    'dedupe_users',
    'employees_search_data',
    'tasks_list_for_employee_data',
    'user_display_name',
    'user_matches_query',
]
