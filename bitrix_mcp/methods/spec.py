from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


AccessLevel = Literal['read', 'write', 'destructive', 'unknown']

DOC_URL_TEMPLATE = 'https://apidocs.bitrix24.ru/api-reference/{path}.html'


@dataclass(frozen=True, slots=True)
class MethodSpec:
    method: str
    scope: str | None
    access: AccessLevel
    access_source: str
    title: str = ''
    doc_path: str = ''
    permission: str = ''
    deprecated: bool = False
    replaced_by: str | None = None
    keywords: tuple[str, ...] = ()
    source: str = 'docs'  # docs | manual | override

    @property
    def doc_url(self) -> str | None:
        if not self.doc_path:
            return None
        path = self.doc_path.removesuffix('.md').removesuffix('.html').lstrip('/')
        return DOC_URL_TEMPLATE.format(path=path)

    def to_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'method': self.method,
            'scope': self.scope,
            'access': self.access,
            'title': self.title,
            'deprecated': self.deprecated,
            'docUrl': self.doc_url,
        }
        if self.replaced_by:
            payload['replacedBy'] = self.replaced_by
        if self.permission:
            payload['permission'] = self.permission
        if self.keywords:
            payload['keywords'] = list(self.keywords)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MethodSpec:
        keywords = data.get('keywords') or []
        return cls(
            method=str(data['method']),
            scope=data.get('scope'),
            access=data.get('access', 'unknown'),
            access_source=str(data.get('access_source') or data.get('accessSource') or 'unknown'),
            title=str(data.get('title') or ''),
            doc_path=str(data.get('doc_path') or data.get('docPath') or ''),
            permission=str(data.get('permission') or ''),
            deprecated=bool(data.get('deprecated', False)),
            replaced_by=data.get('replaced_by') or data.get('replacedBy'),
            keywords=tuple(str(item) for item in keywords),
            source=str(data.get('source') or 'docs'),
        )


@dataclass
class CatalogDocument:
    version: int
    source: dict[str, Any] = field(default_factory=dict)
    source_digest: str = ''
    methods: list[MethodSpec] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
