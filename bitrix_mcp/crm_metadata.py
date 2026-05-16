from __future__ import annotations

import re
from typing import Any

import logfire

from bitrix_mcp.bitrix import BitrixClient
from bitrix_mcp.cache import TTLCache


CRM_ENTITY_FIELDS_METHODS = {
    'lead': 'crm.lead.fields',
    'deal': 'crm.deal.fields',
    'contact': 'crm.contact.fields',
    'company': 'crm.company.fields',
}

CRM_ENTITY_TYPE_IDS = {
    'lead': 1,
    'deal': 2,
    'contact': 3,
    'company': 4,
}

CRM_METADATA_CACHE = TTLCache(ttl_seconds=7 * 24 * 60 * 60)


class CrmFilterResolutionError(ValueError):
    pass


class CrmMetadataResolver:
    def __init__(self, bitrix: BitrixClient):
        self.bitrix = bitrix

    async def resolve_filter(
        self,
        entity: str,
        *,
        filter: dict[str, Any] | None = None,
        conditions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        resolved = await self.resolve_filter_aliases(entity, filter or {})
        for condition in conditions or []:
            resolved.update(await self.resolve_condition(entity, condition))
        return resolved

    async def resolve_filter_aliases(self, entity: str, filter_: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(filter_)

        if entity == 'deal':
            status = pop_first(resolved, ['status', 'STATUS', 'stage_status', 'semantic_status'])
            if status is not None:
                semantic = semantic_value(status)
                if semantic:
                    resolved['STAGE_SEMANTIC_ID'] = semantic
                else:
                    resolved.update(await self.resolve_condition(entity, {'field': 'status', 'value': status}))

            year = pop_first(resolved, ['year', 'YEAR'])
            if year is not None:
                resolved.update(await self.resolve_condition(entity, {'field': 'close date', 'operator': 'year', 'value': year}))

            for key in ['=STAGE_ID', 'STAGE_ID', 'stage_id']:
                if key in resolved and isinstance(resolved[key], str):
                    value = resolved.pop(key)
                    semantic = semantic_value(value)
                    if semantic:
                        resolved['STAGE_SEMANTIC_ID'] = semantic
                    else:
                        resolved.update(await self.resolve_condition(entity, {'field': 'stage', 'value': value}))
                    break

            for key in ['>=CLOSE_DATE', '<=CLOSE_DATE', '>CLOSE_DATE', '<CLOSE_DATE']:
                if key in resolved:
                    resolved[key.replace('CLOSE_DATE', 'CLOSEDATE')] = resolved.pop(key)

        return resolved

    async def resolve_condition(self, entity: str, condition: dict[str, Any]) -> dict[str, Any]:
        field_hint = str(
            condition.get('field')
            or condition.get('name')
            or condition.get('property')
            or ''
        ).strip()
        operator = str(condition.get('operator') or condition.get('op') or '=').strip().lower()
        value = condition.get('value')

        if not field_hint:
            raise CrmFilterResolutionError(f'CRM condition is missing a field: {condition!r}')

        if operator == 'year':
            return self.resolve_year_condition(field_hint, value)

        if entity == 'deal' and normalize_label(field_hint) in {'status', 'stage', 'deal stage'}:
            semantic = semantic_value(value)
            if semantic:
                return {'STAGE_SEMANTIC_ID': semantic}

        try:
            field_name, field_info = await self.resolve_field(entity, field_hint)
        except CrmFilterResolutionError as exc:
            if isinstance(value, str):
                resolved = await self.resolve_status_field_by_value(entity, value)
                if resolved is not None:
                    field_name, status_id = resolved
                    return {self.filter_key(field_name, operator): status_id}
            raise exc

        filter_key = self.filter_key(field_name, operator)

        if field_info.get('type') == 'crm_status':
            status_type = field_info.get('statusType')
            if not status_type:
                raise CrmFilterResolutionError(f'Field "{field_name}" has no statusType metadata.')
            return {filter_key: await self.resolve_status_value(str(status_type), value)}

        if field_name == 'STAGE_SEMANTIC_ID':
            return {filter_key: semantic_value(value)}

        return {filter_key: value}

    async def resolve_status_field_by_value(self, entity: str, value: str) -> tuple[str, str] | None:
        fields = await self.entity_fields(entity)
        matches: list[tuple[str, str]] = []

        for field_name, field_info in fields.items():
            if field_info.get('type') != 'crm_status':
                continue
            status_type = field_info.get('statusType')
            if not status_type:
                continue
            try:
                status_id = await self.resolve_status_value(str(status_type), value)
            except CrmFilterResolutionError:
                continue
            matches.append((field_name, status_id))

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ', '.join(field_name for field_name, _ in matches[:5])
            raise CrmFilterResolutionError(f'Status value "{value}" exists in multiple {entity} fields: {names}.')
        return None

    def resolve_year_condition(self, field_hint: str, value: Any) -> dict[str, Any]:
        year = int(value)
        if year < 2000 or year > 2100:
            raise CrmFilterResolutionError('Year must be between 2000 and 2100.')
        field_name = date_field_name(field_hint)
        return {
            f'>={field_name}': f'{year}-01-01',
            f'<{field_name}': f'{year + 1}-01-01',
        }

    async def resolve_field(self, entity: str, field_hint: str) -> tuple[str, dict[str, Any]]:
        fields = await self.entity_fields(entity)
        hint = normalize_label(field_hint)

        aliases = entity_field_aliases(entity)
        if hint in aliases and aliases[hint] in fields:
            field_name = aliases[hint]
            return field_name, fields[field_name]

        candidates: list[tuple[str, dict[str, Any]]] = []
        for name, info in fields.items():
            labels = {
                normalize_label(name),
                normalize_label(str(info.get('title') or '')),
                normalize_label(str(info.get('upperName') or '')),
            }
            if hint in labels:
                return name, info
            if hint and any(hint in label or label in hint for label in labels if label):
                candidates.append((name, info))

        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            names = ', '.join(name for name, _ in candidates[:5])
            raise CrmFilterResolutionError(f'CRM field "{field_hint}" is ambiguous for {entity}: {names}.')
        raise CrmFilterResolutionError(f'CRM field "{field_hint}" was not found for {entity}.')

    async def resolve_status_value(self, status_type: str, value: Any) -> str:
        if not isinstance(value, str):
            return str(value)

        normalized_value = normalize_label(value)
        semantic = semantic_value(value)
        statuses = await self.status_values_for_type_family(status_type)

        semantic_matches = [
            status for status in statuses
            if semantic and semantic_matches_status(status, semantic)
        ]
        if semantic_matches:
            return str(semantic_matches[0]['STATUS_ID'])

        for status in statuses:
            labels = {
                normalize_label(str(status.get('STATUS_ID') or '')),
                normalize_label(str(status.get('NAME') or '')),
                normalize_label(str(status.get('NAME_INIT') or '')),
            }
            if normalized_value in labels:
                return str(status['STATUS_ID'])

        for status in statuses:
            labels = [
                normalize_label(str(status.get('STATUS_ID') or '')),
                normalize_label(str(status.get('NAME') or '')),
                normalize_label(str(status.get('NAME_INIT') or '')),
            ]
            if any(normalized_value in label or label in normalized_value for label in labels if label):
                return str(status['STATUS_ID'])

        raise CrmFilterResolutionError(f'Status value "{value}" was not found in {status_type}.')

    async def status_values_for_type_family(self, status_type: str) -> list[dict[str, Any]]:
        status_types = await self.status_entity_types()
        family = [status_type]
        entity_type = next((item for item in status_types if item.get('ID') == status_type), None)
        entity_type_id = entity_type.get('ENTITY_TYPE_ID') if entity_type else None

        for item in status_types:
            item_id = item.get('ID')
            if not isinstance(item_id, str):
                continue
            if item_id == status_type:
                continue
            if item.get('PARENT_ID') == status_type:
                family.append(item_id)
            elif entity_type_id is not None and item.get('ENTITY_TYPE_ID') == entity_type_id and item_id.startswith(f'{status_type}_'):
                family.append(item_id)

        values: list[dict[str, Any]] = []
        for type_id in family:
            values.extend(await self.status_values(type_id))
        return values

    async def entity_fields(self, entity: str) -> dict[str, dict[str, Any]]:
        method = CRM_ENTITY_FIELDS_METHODS[entity]
        result = await self.cached_call(f'fields:{entity}', method, {})
        if isinstance(result, dict) and isinstance(result.get('fields'), dict):
            result = result['fields']
        if not isinstance(result, dict):
            raise CrmFilterResolutionError(f'Unexpected fields response for {entity}.')
        return {normalize_field_name(name): dict(info) for name, info in result.items() if isinstance(info, dict)}

    async def status_entity_types(self) -> list[dict[str, Any]]:
        result = await self.cached_call('status_entity_types', 'crm.status.entity.types', {})
        if not isinstance(result, list):
            raise CrmFilterResolutionError('Unexpected crm.status.entity.types response.')
        return [item for item in result if isinstance(item, dict)]

    async def status_values(self, entity_id: str) -> list[dict[str, Any]]:
        result = await self.cached_call(
            f'status_values:{entity_id}',
            'crm.status.list',
            {'order': {'SORT': 'ASC'}, 'filter': {'ENTITY_ID': entity_id}},
        )
        if not isinstance(result, list):
            raise CrmFilterResolutionError(f'Unexpected crm.status.list response for {entity_id}.')
        return [item for item in result if isinstance(item, dict)]

    async def cached_call(self, key: str, method: str, params: dict[str, Any]) -> Any:
        cache_key = CRM_METADATA_CACHE.make_key(key, params)
        cached = CRM_METADATA_CACHE.get(cache_key)
        if cached is not None:
            logfire.info('Bitrix CRM metadata cache hit', metadata_key=key)
            return cached

        logfire.info('Loading Bitrix CRM metadata', metadata_key=key, bitrix_method=method)
        result = await self.bitrix.call_method(method, params)
        CRM_METADATA_CACHE.set(cache_key, result)
        return result

    @staticmethod
    def filter_key(field_name: str, operator: str) -> str:
        if operator in {'=', 'eq', 'is'}:
            return field_name
        if operator in {'!=', 'ne', 'not'}:
            return f'!{field_name}'
        if operator in {'contains', 'like'}:
            return f'%{field_name}'
        if operator in {'>=', '=>', 'from', 'after_or_equal'}:
            return f'>={field_name}'
        if operator in {'>', 'after'}:
            return f'>{field_name}'
        if operator in {'<=', '=<', 'to', 'before_or_equal'}:
            return f'<={field_name}'
        if operator in {'<', 'before'}:
            return f'<{field_name}'
        return field_name


def entity_field_aliases(entity: str) -> dict[str, str]:
    common = {
        'responsible': 'ASSIGNED_BY_ID',
        'assigned': 'ASSIGNED_BY_ID',
        'assigned by': 'ASSIGNED_BY_ID',
    }
    if entity == 'deal':
        return {
            **common,
            'status': 'STAGE_ID',
            'stage': 'STAGE_ID',
            'deal stage': 'STAGE_ID',
            'semantic status': 'STAGE_SEMANTIC_ID',
            'close date': 'CLOSEDATE',
            'closed date': 'CLOSEDATE',
            'closed at': 'CLOSEDATE',
            'date': 'CLOSEDATE',
            'year': 'CLOSEDATE',
        }
    if entity == 'company':
        return {
            **common,
            'type': 'COMPANY_TYPE',
            'company type': 'COMPANY_TYPE',
            'industry': 'INDUSTRY',
        }
    if entity == 'contact':
        return {**common, 'type': 'TYPE_ID', 'contact type': 'TYPE_ID'}
    if entity == 'lead':
        return {**common, 'status': 'STATUS_ID', 'source': 'SOURCE_ID'}
    return common


def date_field_name(field_hint: str) -> str:
    normalized = normalize_label(field_hint)
    if normalized in {'created', 'created date', 'created time', 'date create', 'created at'}:
        return 'DATE_CREATE'
    if normalized in {'modified', 'modified date', 'updated', 'updated date'}:
        return 'DATE_MODIFY'
    return 'CLOSEDATE'


def semantic_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = normalize_label(value)
    if normalized in {'won', 'success', 'successful', 'closed won'}:
        return 'S'
    if normalized in {'lost', 'failed', 'failure', 'closed lost'}:
        return 'F'
    if normalized in {'open', 'active', 'in progress', 'process'}:
        return 'P'
    return None


def semantic_matches_status(status: dict[str, Any], semantic: str) -> bool:
    if status.get('SEMANTICS') == semantic:
        return True
    extra = status.get('EXTRA')
    extra_semantics = extra.get('SEMANTICS') if isinstance(extra, dict) else None
    return {
        'S': 'success',
        'F': 'failure',
        'P': 'process',
    }.get(semantic) == extra_semantics


def pop_first(values: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in values:
            return values.pop(key)
    return None


def normalize_field_name(name: str) -> str:
    if name.isupper():
        return name
    snake = re.sub(r'(?<!^)(?=[A-Z])', '_', name).upper()
    return snake


def normalize_label(value: str) -> str:
    value = value.strip().lower().replace('_', ' ').replace('-', ' ')
    return ' '.join(value.split())
