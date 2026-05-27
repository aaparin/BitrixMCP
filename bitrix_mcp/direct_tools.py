from __future__ import annotations

import json
import re
from typing import Any

from bitrix_mcp.bitrix import BitrixApiError, BitrixClient


STATIC_CRM_ENTITIES: dict[str, dict[str, str]] = {
    'LEAD': {
        'fields': 'crm.lead.fields',
        'userfield_list': 'crm.lead.userfield.list',
        'userfield_get': 'crm.lead.userfield.get',
    },
    'DEAL': {
        'fields': 'crm.deal.fields',
        'userfield_list': 'crm.deal.userfield.list',
        'userfield_get': 'crm.deal.userfield.get',
    },
    'COMPANY': {
        'fields': 'crm.company.fields',
        'userfield_list': 'crm.company.userfield.list',
        'userfield_get': 'crm.company.userfield.get',
    },
    'CONTACT': {
        'fields': 'crm.contact.fields',
        'userfield_list': 'crm.contact.userfield.list',
        'userfield_get': 'crm.contact.userfield.get',
    },
    'QUOTE': {
        'fields': 'crm.quote.fields',
        'userfield_list': 'crm.quote.userfield.list',
        'userfield_get': 'crm.quote.userfield.get',
    },
}

ENTITY_ALIASES = {
    'CRM_LEAD': 'LEAD',
    'LEADS': 'LEAD',
    'CRM_DEAL': 'DEAL',
    'DEALS': 'DEAL',
    'CRM_COMPANY': 'COMPANY',
    'COMPANIES': 'COMPANY',
    'CRM_CONTACT': 'CONTACT',
    'CONTACTS': 'CONTACT',
    'CRM_QUOTE': 'QUOTE',
    'QUOTES': 'QUOTE',
}


def parse_object_input(value: dict[str, Any] | str | None, *, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BitrixApiError(f'{name} must be a JSON object.') from exc
        if isinstance(parsed, dict):
            return parsed
    raise BitrixApiError(f'{name} must be an object.')


def parse_string_list_input(value: list[str] | str | None, *, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith('['):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise BitrixApiError(f'{name} must be a JSON array of strings.') from exc
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in stripped.split(',') if part.strip()]
    raise BitrixApiError(f'{name} must be an array of strings.')


def normalize_entity_name(entity: str) -> str:
    normalized = entity.strip().upper().replace('-', '_').replace(' ', '_')
    return ENTITY_ALIASES.get(normalized, normalized)


async def paginate_bitrix_list(
    bitrix: BitrixClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    max_pages: int = 200,
) -> list[dict[str, Any]]:
    request_params = dict(params or {})
    request_params.setdefault('start', 0)
    items: list[dict[str, Any]] = []

    for _ in range(max_pages):
        payload = await bitrix.call_method_payload(method, request_params)
        result = payload.get('result') if isinstance(payload, dict) else payload
        page_items = extract_list_items(result)
        items.extend(item for item in page_items if isinstance(item, dict))

        next_start = None
        if isinstance(payload, dict):
            next_start = payload.get('next')
        if next_start is None and isinstance(result, dict):
            next_start = result.get('next')
        if next_start is None:
            break
        request_params['start'] = next_start
    else:
        raise BitrixApiError(f'Pagination for {method} exceeded {max_pages} pages.')

    return items


def extract_list_items(result: Any) -> list[Any]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ('items', 'types', 'userFields'):
            value = result.get(key)
            if isinstance(value, list):
                return value
    return []


async def resolve_crm_entity(bitrix: BitrixClient, entity: str) -> dict[str, Any]:
    normalized = normalize_entity_name(entity)
    if normalized in STATIC_CRM_ENTITIES:
        return {'name': normalized, 'kind': 'static', **STATIC_CRM_ENTITIES[normalized]}

    match = re.fullmatch(r'(?:CRM_)?DYNAMIC_(\d+)', normalized)
    if not match:
        raise BitrixApiError(f'Unsupported CRM entity "{entity}".')

    requested_id = int(match.group(1))
    types = await paginate_bitrix_list(bitrix, 'crm.type.list', {})
    for crm_type in types:
        entity_type_id = crm_type.get('entityTypeId') or crm_type.get('ENTITY_TYPE_ID')
        type_id = crm_type.get('id') or crm_type.get('ID')
        if str(entity_type_id) == str(requested_id) or str(type_id) == str(requested_id):
            return {
                'name': normalized,
                'kind': 'dynamic',
                'entityTypeId': int(entity_type_id or requested_id),
                'type': compact_dynamic_type(crm_type),
            }

    raise BitrixApiError(f'Smart process "{entity}" was not found in crm.type.list.')


def compact_dynamic_type(crm_type: dict[str, Any]) -> dict[str, Any]:
    return {
        key: crm_type.get(key)
        for key in ('id', 'entityTypeId', 'title', 'code', 'name')
        if crm_type.get(key) is not None
    }


async def get_crm_fields(bitrix: BitrixClient, resolved: dict[str, Any]) -> dict[str, Any]:
    if resolved['kind'] == 'dynamic':
        payload = await bitrix.call_method_payload('crm.item.fields', {'entityTypeId': resolved['entityTypeId']})
        result = payload.get('result') if isinstance(payload, dict) else payload
        if isinstance(result, dict) and isinstance(result.get('fields'), dict):
            return result['fields']
        if isinstance(result, dict):
            return result
        return {}

    result = await bitrix.call_method(resolved['fields'], {})
    return result if isinstance(result, dict) else {}


async def list_crm_userfields(bitrix: BitrixClient, resolved: dict[str, Any]) -> list[dict[str, Any]]:
    if resolved['kind'] == 'dynamic':
        fields = await get_crm_fields(bitrix, resolved)
        return [
            {'FIELD_NAME': name, **definition}
            for name, definition in fields.items()
            if name.startswith('UF_') and isinstance(definition, dict)
        ]
    return await paginate_bitrix_list(bitrix, resolved['userfield_list'], {})


async def enrich_userfield_from_get(
    bitrix: BitrixClient,
    resolved: dict[str, Any],
    field: dict[str, Any],
) -> dict[str, Any]:
    if resolved['kind'] == 'dynamic':
        return field
    field_id = field.get('ID') or field.get('id')
    if not field_id:
        return field
    try:
        result = await bitrix.call_method(resolved['userfield_get'], {'ID': int(field_id)})
    except (TypeError, ValueError):
        return field
    return result if isinstance(result, dict) else field


def label_from_userfield(field: dict[str, Any]) -> str | None:
    for key in ('EDIT_FORM_LABEL', 'LIST_COLUMN_LABEL', 'LIST_FILTER_LABEL', 'label', 'title', 'formLabel'):
        value = field.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for lang in ('en', 'EN', 'ru', 'RU', 'lv', 'LV'):
                label = value.get(lang)
                if isinstance(label, str) and label.strip():
                    return label.strip()
    return None


def normalize_enum_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        iterable = value.values()
    elif isinstance(value, list):
        iterable = value
    else:
        return []

    items: list[dict[str, Any]] = []
    for item in iterable:
        if not isinstance(item, dict):
            continue
        compact = {
            key: item.get(key)
            for key in ('ID', 'id', 'VALUE', 'value', 'XML_ID', 'xmlId', 'SORT', 'sort')
            if item.get(key) is not None
        }
        if compact:
            items.append(compact)
    return items


def normalize_userfield(field: dict[str, Any], *, include_enums: bool) -> dict[str, Any]:
    field_name = field.get('FIELD_NAME') or field.get('fieldName') or field.get('name')
    normalized: dict[str, Any] = {
        'FIELD_NAME': field_name,
        'ID': field.get('ID') or field.get('id'),
        'label': label_from_userfield(field),
        'type': field.get('USER_TYPE_ID') or field.get('userTypeId') or field.get('type'),
        'multiple': field.get('MULTIPLE') or field.get('isMultiple'),
        'mandatory': field.get('MANDATORY') or field.get('isRequired'),
        'xmlId': field.get('XML_ID') or field.get('xmlId'),
    }
    normalized = {key: value for key, value in normalized.items() if value not in (None, '')}
    if include_enums:
        enum = normalize_enum_items(field.get('LIST') or field.get('items') or field.get('enum'))
        if enum:
            normalized['enum'] = enum
    return normalized


def normalize_field(name: str, field: dict[str, Any], *, include_enums: bool) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        'name': name,
        'label': label_from_userfield(field) or field.get('title') or field.get('listLabel') or field.get('formLabel'),
        'type': field.get('type') or field.get('USER_TYPE_ID') or field.get('userTypeId'),
        'required': field.get('isRequired') or field.get('MANDATORY'),
        'readOnly': field.get('isReadOnly'),
        'multiple': field.get('isMultiple') or field.get('MULTIPLE'),
        'userField': field.get('isUserField') or name.startswith('UF_'),
    }
    normalized = {key: value for key, value in normalized.items() if value not in (None, '')}
    if include_enums:
        enum = normalize_enum_items(field.get('items') or field.get('LIST') or field.get('enum'))
        if enum:
            normalized['enum'] = enum
    return normalized


async def describe_crm_entity_fields(
    bitrix: BitrixClient,
    entity: str,
    field_names: list[str] | None = None,
    *,
    include_enums: bool = True,
) -> dict[str, Any]:
    resolved = await resolve_crm_entity(bitrix, entity)
    wanted = {field_name.upper() for field_name in (field_names or [])}

    fields = await get_crm_fields(bitrix, resolved)
    normalized_fields = [
        normalize_field(name, definition, include_enums=include_enums)
        for name, definition in fields.items()
        if isinstance(definition, dict) and (not wanted or name.upper() in wanted)
    ]

    userfields = await list_crm_userfields(bitrix, resolved)
    matched_userfields: list[dict[str, Any]] = []
    for field in userfields:
        field_name = str(field.get('FIELD_NAME') or field.get('fieldName') or field.get('name') or '')
        if wanted and field_name.upper() not in wanted:
            continue
        detailed = field
        if include_enums:
            normalized = normalize_userfield(field, include_enums=True)
            if not normalized.get('enum'):
                detailed = await enrich_userfield_from_get(bitrix, resolved, field)
        matched_userfields.append(normalize_userfield(detailed, include_enums=include_enums))

    return {
        'entity': entity,
        'resolved': {key: value for key, value in resolved.items() if key not in {'fields', 'userfield_list', 'userfield_get'}},
        'fields': normalized_fields,
        'userFields': matched_userfields,
    }
