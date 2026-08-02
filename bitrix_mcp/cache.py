from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class CacheEntry:
    expires_at: float
    value: Any


class TTLCache:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, CacheEntry] = {}

    def make_key(self, namespace: str, payload: Any, *, scope: str = '') -> str:
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
        if scope:
            return f'{scope}:{namespace}:{serialized}'
        return f'{namespace}:{serialized}'

    def get(self, key: str) -> Any | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._items.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: Any) -> None:
        self._items[key] = CacheEntry(
            expires_at=time.monotonic() + self.ttl_seconds,
            value=value,
        )

    def delete(self, key: str) -> bool:
        return self._items.pop(key, None) is not None

    def invalidate_prefix(self, prefix: str) -> int:
        keys = [key for key in self._items if key.startswith(prefix)]
        for key in keys:
            self._items.pop(key, None)
        return len(keys)

    def clear(self) -> None:
        self._items.clear()
