from __future__ import annotations

import json
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from bitrix_mcp.methods.spec import AccessLevel, MethodSpec

PACKAGE_DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
DEFAULT_CATALOG_PATH = PACKAGE_DATA_DIR / 'method_catalog.json'


def normalize_method_name(method: str) -> str:
    return method.strip().lower().replace('-', '.')


class MethodCatalog:
    def __init__(self, methods: Iterable[MethodSpec], *, source: dict[str, Any] | None = None):
        specs = list(methods)
        self._by_name: dict[str, MethodSpec] = {normalize_method_name(spec.method): spec for spec in specs}
        self._methods = sorted(specs, key=lambda item: item.method.lower())
        self.source = source or {}

    @classmethod
    def from_path(cls, path: str | Path) -> MethodCatalog:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        methods = [MethodSpec.from_dict(item) for item in payload.get('methods', [])]
        return cls(methods, source=payload.get('source') or {})

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MethodCatalog:
        methods = [MethodSpec.from_dict(item) for item in payload.get('methods', [])]
        return cls(methods, source=payload.get('source') or {})

    def __len__(self) -> int:
        return len(self._methods)

    def get(self, method: str) -> MethodSpec | None:
        return self._by_name.get(normalize_method_name(method))

    def all_methods(self) -> list[MethodSpec]:
        return list(self._methods)

    def scopes(self) -> list[str]:
        values = {spec.scope for spec in self._methods if spec.scope}
        return sorted(values)

    def suggest(self, method: str, *, limit: int = 5) -> list[str]:
        needle = normalize_method_name(method)
        names = [spec.method for spec in self._methods]
        return get_close_matches(needle, names, n=limit, cutoff=0.5)

    def search(
        self,
        query: str = '',
        *,
        scope: str | None = None,
        access: AccessLevel | None = None,
        include_deprecated: bool = True,
        limit: int = 50,
    ) -> list[MethodSpec]:
        limit = max(1, min(int(limit), 200))
        tokens = [token for token in normalize_method_name(query).replace('_', '.').split('.') if token]
        if query and not tokens:
            tokens = [normalize_method_name(query)]

        results: list[tuple[int, MethodSpec]] = []
        for spec in self._methods:
            if scope and (spec.scope or '').lower() != scope.lower():
                continue
            if access and spec.access != access:
                continue
            if not include_deprecated and spec.deprecated:
                continue

            haystack = ' '.join(
                [
                    spec.method.lower(),
                    (spec.scope or '').lower(),
                    spec.title.lower(),
                    ' '.join(spec.keywords).lower(),
                    ' '.join(spec.method.lower().split('.')),
                ]
            )
            if tokens and not all(token in haystack for token in tokens):
                continue

            score = 0
            method_lower = spec.method.lower()
            if query and method_lower == normalize_method_name(query):
                score += 100
            for token in tokens:
                if method_lower.startswith(token) or f'.{token}' in f'.{method_lower}':
                    score += 10
                elif token in method_lower:
                    score += 5
                elif token in spec.title.lower():
                    score += 2
            results.append((score, spec))

        results.sort(key=lambda item: (-item[0], item[1].method.lower()))
        return [spec for _, spec in results[:limit]]


@lru_cache(maxsize=4)
def _load_cached_catalog(path: str) -> MethodCatalog:
    return MethodCatalog.from_path(path)


def default_catalog(path: str | Path | None = None) -> MethodCatalog:
    catalog_path = Path(path) if path else DEFAULT_CATALOG_PATH
    if not catalog_path.exists():
        raise FileNotFoundError(
            f'Method catalog not found at {catalog_path}. '
            'Run scripts/generate_method_catalog.py or set BITRIX_METHOD_CATALOG_PATH.'
        )
    return _load_cached_catalog(str(catalog_path.resolve()))
