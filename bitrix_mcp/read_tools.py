from __future__ import annotations

import re
from typing import Any

from bitrix_mcp.bitrix import BitrixApiError, BitrixClient
from bitrix_mcp.direct_tools import parse_object_input, parse_string_list_input


CRM_ENTITY_TYPE_IDS = {
    'LEAD': 1,
    'DEAL': 2,
    'CONTACT': 3,
    'COMPANY': 4,
    'QUOTE': 7,
    'INVOICE': 31,
}

CRM_ENTITY_ALIASES = {
    'LEADS': 'LEAD',
    'CRM_LEAD': 'LEAD',
    'CRM_LEADS': 'LEAD',
    'DEALS': 'DEAL',
    'CRM_DEAL': 'DEAL',
    'CRM_DEALS': 'DEAL',
    'CONTACTS': 'CONTACT',
    'CRM_CONTACT': 'CONTACT',
    'CRM_CONTACTS': 'CONTACT',
    'COMPANIES': 'COMPANY',
    'CRM_COMPANY': 'COMPANY',
    'CRM_COMPANIES': 'COMPANY',
    'QUOTES': 'QUOTE',
    'CRM_QUOTE': 'QUOTE',
    'CRM_QUOTES': 'QUOTE',
    'INVOICES': 'INVOICE',
    'CRM_INVOICE': 'INVOICE',
    'CRM_INVOICES': 'INVOICE',
}

DEFAULT_TASK_SELECT = [
    'ID',
    'TITLE',
    'STATUS',
    'PRIORITY',
    'RESPONSIBLE_ID',
    'CREATED_BY',
    'CREATED_DATE',
    'DEADLINE',
    'CLOSED_DATE',
    'UF_CRM_TASK',
]

DEFAULT_ACTIVITY_SELECT = [
    'ID',
    'OWNER_ID',
    'OWNER_TYPE_ID',
    'TYPE_ID',
    'PROVIDER_ID',
    'PROVIDER_TYPE_ID',
    'SUBJECT',
    'START_TIME',
    'END_TIME',
    'DEADLINE',
    'COMPLETED',
    'STATUS',
    'RESPONSIBLE_ID',
    'DIRECTION',
]

DEFAULT_EMPLOYEE_SELECT = [
    'ID',
    'ACTIVE',
    'NAME',
    'LAST_NAME',
    'SECOND_NAME',
    'EMAIL',
    'PERSONAL_MOBILE',
    'WORK_PHONE',
    'WORK_POSITION',
    'UF_DEPARTMENT',
    'UF_PHONE_INNER',
    'USER_TYPE',
]


def resolve_crm_entity_type_id(entity: str | int) -> int:
    if isinstance(entity, bool):
        raise BitrixApiError('CRM entity must be a name or positive entity type ID.')
    if isinstance(entity, int):
        if entity > 0:
            return entity
        raise BitrixApiError('CRM entity type ID must be positive.')

    normalized = entity.strip().upper().replace('-', '_').replace(' ', '_')
    if normalized.isdigit() and int(normalized) > 0:
        return int(normalized)
    normalized = CRM_ENTITY_ALIASES.get(normalized, normalized)
    if normalized in CRM_ENTITY_TYPE_IDS:
        return CRM_ENTITY_TYPE_IDS[normalized]

    dynamic_match = re.fullmatch(r'(?:CRM_)?DYNAMIC_(\d+)', normalized)
    if dynamic_match and int(dynamic_match.group(1)) > 0:
        return int(dynamic_match.group(1))

    supported = ', '.join(name.lower() for name in CRM_ENTITY_TYPE_IDS)
    raise BitrixApiError(
        f'Unsupported CRM entity "{entity}". Use one of {supported}, a numeric entityTypeId, '
        'or DYNAMIC_<entityTypeId>.'
    )


def normalize_start(start: int) -> int:
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise BitrixApiError('start must be a non-negative integer.')
    return start


def add_optional_object(
    params: dict[str, Any],
    key: str,
    value: dict[str, Any] | str | None,
) -> None:
    if value is not None:
        params[key] = parse_object_input(value, name=key)


def add_optional_select(
    params: dict[str, Any],
    select: list[str] | str | None,
    *,
    default: list[str] | None = None,
) -> None:
    if select is None:
        if default is not None:
            params['select'] = default.copy()
        return
    parsed = parse_string_list_input(select, name='select')
    if parsed:
        params['select'] = parsed


def ensure_payload_dict(payload: Any) -> dict[str, Any]:
    return payload if isinstance(payload, dict) else {'result': payload}


async def crm_types_list_data(
    bitrix: BitrixClient,
    *,
    filter: dict[str, Any] | str | None = None,
    order: dict[str, Any] | str | None = None,
    start: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {'start': normalize_start(start)}
    add_optional_object(params, 'filter', filter)
    add_optional_object(params, 'order', order)
    return ensure_payload_dict(await bitrix.call_method_payload('crm.type.list', params))


async def crm_items_list_data(
    bitrix: BitrixClient,
    entity: str | int,
    *,
    filter: dict[str, Any] | str | None = None,
    select: list[str] | str | None = None,
    order: dict[str, Any] | str | None = None,
    start: int = 0,
    use_original_userfield_names: bool = True,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        'entityTypeId': resolve_crm_entity_type_id(entity),
        'start': normalize_start(start),
        'useOriginalUfNames': 'Y' if use_original_userfield_names else 'N',
    }
    add_optional_object(params, 'filter', filter)
    add_optional_object(params, 'order', order)
    add_optional_select(params, select)
    return ensure_payload_dict(await bitrix.call_method_payload('crm.item.list', params))


async def crm_item_get_data(
    bitrix: BitrixClient,
    entity: str | int,
    item_id: int,
    *,
    use_original_userfield_names: bool = True,
) -> dict[str, Any]:
    if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
        raise BitrixApiError('item_id must be a positive integer.')
    params = {
        'entityTypeId': resolve_crm_entity_type_id(entity),
        'id': item_id,
        'useOriginalUfNames': 'Y' if use_original_userfield_names else 'N',
    }
    return ensure_payload_dict(await bitrix.call_method_payload('crm.item.get', params))


async def tasks_list_data(
    bitrix: BitrixClient,
    *,
    filter: dict[str, Any] | str | None = None,
    select: list[str] | str | None = None,
    order: dict[str, Any] | str | None = None,
    start: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {'start': normalize_start(start)}
    add_optional_object(params, 'filter', filter)
    add_optional_object(params, 'order', order)
    add_optional_select(params, select, default=DEFAULT_TASK_SELECT)
    return ensure_payload_dict(await bitrix.call_method_payload('tasks.task.list', params))


async def task_get_data(
    bitrix: BitrixClient,
    task_id: int,
    *,
    select: list[str] | str | None = None,
) -> dict[str, Any]:
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        raise BitrixApiError('task_id must be a positive integer.')
    params: dict[str, Any] = {'taskId': task_id}
    add_optional_select(params, select)
    return ensure_payload_dict(await bitrix.call_method_payload('tasks.task.get', params))


async def activities_list_data(
    bitrix: BitrixClient,
    *,
    filter: dict[str, Any] | str | None = None,
    select: list[str] | str | None = None,
    order: dict[str, Any] | str | None = None,
    start: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {'start': normalize_start(start)}
    add_optional_object(params, 'filter', filter)
    add_optional_object(params, 'order', order)
    add_optional_select(params, select, default=DEFAULT_ACTIVITY_SELECT)
    return ensure_payload_dict(await bitrix.call_method_payload('crm.activity.list', params))


async def activity_get_data(bitrix: BitrixClient, activity_id: int) -> dict[str, Any]:
    if isinstance(activity_id, bool) or not isinstance(activity_id, int) or activity_id <= 0:
        raise BitrixApiError('activity_id must be a positive integer.')
    return ensure_payload_dict(
        await bitrix.call_method_payload('crm.activity.get', {'id': activity_id})
    )


async def employees_list_data(
    bitrix: BitrixClient,
    *,
    filter: dict[str, Any] | str | None = None,
    select: list[str] | str | None = None,
    sort: str = 'ID',
    order: str = 'ASC',
    start: int = 0,
) -> dict[str, Any]:
    normalized_order = order.strip().upper()
    if normalized_order not in {'ASC', 'DESC'}:
        raise BitrixApiError('order must be ASC or DESC.')
    params: dict[str, Any] = {
        'sort': sort.strip() or 'ID',
        'order': normalized_order,
        'start': normalize_start(start),
    }
    add_optional_object(params, 'filter', filter)
    add_optional_select(params, select, default=DEFAULT_EMPLOYEE_SELECT)
    return ensure_payload_dict(await bitrix.call_method_payload('user.get', params))


async def employee_get_data(bitrix: BitrixClient, employee_id: int) -> dict[str, Any]:
    if isinstance(employee_id, bool) or not isinstance(employee_id, int) or employee_id <= 0:
        raise BitrixApiError('employee_id must be a positive integer.')
    return ensure_payload_dict(
        await bitrix.call_method_payload(
            'user.get',
            {
                'filter': {'ID': employee_id},
                'select': DEFAULT_EMPLOYEE_SELECT.copy(),
            },
        )
    )


async def telephony_calls_list_data(
    bitrix: BitrixClient,
    *,
    filter: dict[str, Any] | str | None = None,
    sort: str = 'CALL_START_DATE',
    order: str = 'DESC',
    start: int = 0,
) -> dict[str, Any]:
    normalized_order = order.strip().upper()
    if normalized_order not in {'ASC', 'DESC'}:
        raise BitrixApiError('order must be ASC or DESC.')
    params: dict[str, Any] = {
        'FILTER': parse_object_input(filter, name='filter'),
        'SORT': sort.strip() or 'CALL_START_DATE',
        'ORDER': normalized_order,
        'start': normalize_start(start),
    }
    return ensure_payload_dict(
        await bitrix.call_method_payload('voximplant.statistic.get', params)
    )


async def telephony_lines_list_data(bitrix: BitrixClient) -> dict[str, Any]:
    return ensure_payload_dict(await bitrix.call_method_payload('voximplant.line.get', {}))
