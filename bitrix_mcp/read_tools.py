from __future__ import annotations

import re
from typing import Any, Literal

from bitrix_mcp.bitrix import BitrixApiError, BitrixClient
from bitrix_mcp.crm_metadata import CrmFilterResolutionError, CrmMetadataResolver
from bitrix_mcp.direct_tools import parse_object_input, parse_object_list_input, parse_string_list_input
from bitrix_mcp.records import normalize_text


CRM_ENTITY_METHODS = {
    'company': 'crm.company.list',
    'contact': 'crm.contact.list',
    'deal': 'crm.deal.list',
    'lead': 'crm.lead.list',
}

TASK_STATUS_ALIASES = {
    'new': 1,
    'pending': 2,
    'accepted': 2,
    'in_progress': 3,
    'in progress': 3,
    'active': 3,
    'current': 3,
    'almost completed': 4,
    'awaiting review': 4,
    'completed': 5,
    'finished': 5,
    'done': 5,
    'deferred': 6,
}


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


#: Operator prefixes a Bitrix24 filter key may carry, longest first so that
#: `!><` is not mistaken for `!`.
FILTER_OPERATORS = ('!><', '!%', '!=', '!@', '>=', '<=', '=%', '%=', '><', '!', '=', '>', '<', '%', '@')


def split_filter_operator(key: str) -> tuple[str, str]:
    """`">=ID"` → `(">=", "ID")`. A key with no operator keeps an empty one."""
    for operator in FILTER_OPERATORS:
        if key.startswith(operator):
            return operator, key[len(operator) :]
    return '', key


def to_camel_field(name: str) -> str:
    """`STAGE_ID` → `stageId`. Anything already camel, or a user field, is left alone.

    The two halves of the CRM API disagree about names: `crm.deal.fields` — which
    is what `crm_describe_fields` reports — speaks UPPER_SNAKE, while
    `crm.item.*` speaks camelCase. A caller that looks a field up and then uses
    it in a filter is therefore wrong through no fault of its own, and Bitrix
    answers with `field '%_TI_TL_E' is not allowed in filter`: its own converter
    mangles the name it was given.

    Translating here is the adapter's job. `UF_` names are exempt: those are the
    portal's own, and Bitrix does not rename them.
    """
    if not name or not name.isupper() or name.startswith('UF_'):
        return name
    head, *rest = name.lower().split('_')
    return head + ''.join(part.capitalize() for part in rest)


def camelize_keys(value: Any) -> Any:
    """Filter or order object with its field names in the dialect crm.item speaks."""
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        operator, field = split_filter_operator(str(key))
        out[f'{operator}{to_camel_field(field)}'] = item
    return out


def camelize_select(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [to_camel_field(str(item)) if item != '*' else '*' for item in value]


async def crm_items_list_data(
    bitrix: BitrixClient,
    entity: str | int,
    *,
    filter: dict[str, Any] | str | None = None,
    select: list[str] | str | None = None,
    order: dict[str, Any] | str | None = None,
    start: int = 0,
    use_original_userfield_names: bool | None = None,
) -> dict[str, Any]:
    """List CRM items.

    `use_original_userfield_names` is tri-state on purpose. Bitrix24 answers
    `crm.item.list` with EMPTY item objects when `useOriginalUfNames=Y` is
    combined with a `select` — no error, no warning, just `{}` per record. The
    old default of True therefore broke every selective read silently, and the
    only way to get data back was to ask for all ~85 fields of every record.

    So: unspecified means "choose the one that works" — original UF names when
    no select is given, auto names when one is. Asking for both explicitly is
    refused rather than silently answered with nothing.
    """
    params: dict[str, Any] = {
        'entityTypeId': resolve_crm_entity_type_id(entity),
        'start': normalize_start(start),
    }
    add_optional_object(params, 'filter', filter)
    add_optional_object(params, 'order', order)
    add_optional_select(params, select)
    # crm.item.* is camelCase; crm_describe_fields reports UPPER_SNAKE. A caller
    # that looks a field up and uses it here is otherwise punished for it.
    if 'filter' in params:
        params['filter'] = camelize_keys(params['filter'])
    if 'order' in params:
        params['order'] = camelize_keys(params['order'])
    if 'select' in params:
        params['select'] = camelize_select(params['select'])
    has_select = bool(params.get('select'))

    if use_original_userfield_names is None:
        use_original_userfield_names = not has_select
    elif use_original_userfield_names and has_select:
        raise BitrixApiError(
            'Bitrix24 returns empty records for crm.item.list when '
            'use_original_userfield_names=true is combined with select. Either drop '
            'select to get every field under UF_CRM_* names, or keep select and let '
            'use_original_userfield_names default (custom fields then come back under '
            'their auto names).'
        )

    params['useOriginalUfNames'] = 'Y' if use_original_userfield_names else 'N'
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


def normalize_task_status_filter(status: str | int | None) -> dict[str, Any]:
    if status is None:
        return {}
    if isinstance(status, int):
        return {'STATUS': status}
    normalized = normalize_text(status)
    if normalized in {'open', 'not completed', 'non completed', 'not done'}:
        return {'!STATUS': 5}
    if normalized in TASK_STATUS_ALIASES:
        return {'STATUS': TASK_STATUS_ALIASES[normalized]}
    if status.isdigit():
        return {'STATUS': int(status)}
    return {'STATUS': status}


async def crm_count_data(
    bitrix: BitrixClient,
    entity: Literal['company', 'contact', 'deal', 'lead'] | str,
    *,
    filter: dict[str, Any] | str | None = None,
    conditions: list[dict[str, Any]] | str | None = None,
) -> dict[str, Any]:
    entity_key = str(entity).strip().lower()
    if entity_key not in CRM_ENTITY_METHODS:
        supported = ', '.join(sorted(CRM_ENTITY_METHODS))
        raise BitrixApiError(f'Unsupported CRM entity "{entity}". Use one of: {supported}.')

    resolver = CrmMetadataResolver(bitrix)
    try:
        normalized_filter = await resolver.resolve_filter(
            entity_key,
            filter=parse_object_input(filter, name='filter'),
            conditions=parse_object_list_input(conditions, name='conditions'),
        )
    except CrmFilterResolutionError as exc:
        raise BitrixApiError(str(exc)) from exc

    method = CRM_ENTITY_METHODS[entity_key]
    total = await bitrix.count_list_method(method, {'filter': normalized_filter, 'select': ['ID'], 'start': 0})
    return {'entity': entity_key, 'filter': normalized_filter, 'total': total}


async def tasks_count_data(
    bitrix: BitrixClient,
    *,
    filter: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    parsed_filter = parse_object_input(filter, name='filter')
    total = await bitrix.count_list_method(
        'tasks.task.list',
        {'filter': parsed_filter, 'select': ['ID'], 'start': 0},
    )
    return {'entity': 'task', 'filter': parsed_filter, 'total': total}
