from __future__ import annotations

from typing import Any


def normalize_text(value: str) -> str:
    return ' '.join(value.strip().lower().split())


def extract_records(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ['tasks', 'items', 'companies', 'contacts', 'deals', 'leads']:
            items = value.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return None


def first_record(value: Any) -> dict[str, Any] | None:
    records = extract_records(value)
    if records:
        return records[0]
    return None


def compact_records(value: Any, *, limit: int = 10) -> Any:
    records = extract_records(value)
    if records is None:
        return value
    compacted = records[:limit]
    if len(records) > limit:
        return {
            'items': compacted,
            'truncated': True,
            'returned': limit,
            'available_in_response': len(records),
        }
    return compacted


def user_display_name(user: dict[str, Any] | None) -> str | None:
    if not user:
        return None
    name = ' '.join(
        part.strip()
        for part in [str(user.get('NAME') or ''), str(user.get('LAST_NAME') or '')]
        if part and part.strip()
    )
    return name or user.get('EMAIL') or user.get('ID')
