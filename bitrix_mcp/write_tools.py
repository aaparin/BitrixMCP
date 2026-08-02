from __future__ import annotations

from typing import Any

from bitrix_mcp.bitrix import BitrixApiError, BitrixClient
from bitrix_mcp.direct_tools import parse_object_input
from bitrix_mcp.read_tools import resolve_crm_entity_type_id


async def _dry_run_preview(
    bitrix: BitrixClient,
    method: str,
    params: dict[str, Any],
    *,
    commit_hint: str,
) -> dict[str, Any]:
    ownership_payload: dict[str, Any] | None = None
    final_params = params
    if bitrix.ownership_guard is not None:
        ownership = await bitrix.ownership_guard.check(bitrix, method, params, bitrix.identity)
        if ownership.mutated_params is not None:
            final_params = ownership.mutated_params
        ownership_payload = {
            'allowed': ownership.allowed,
            'result': ownership.result,
            'reason': ownership.reason,
            'ownerName': ownership.owner_name,
        }
        if not ownership.allowed:
            return {
                'dryRun': True,
                'method': method,
                'params': final_params,
                'ownership': ownership_payload,
                'error': 'ownership_denied',
                'message': ownership.reason or 'Ownership check failed.',
            }
    return {
        'dryRun': True,
        'method': method,
        'params': final_params,
        'ownership': ownership_payload,
        'message': commit_hint,
    }


async def crm_item_add_data(
    bitrix: BitrixClient,
    entity: str | int,
    fields: dict[str, Any] | str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    entity_type_id = resolve_crm_entity_type_id(entity)
    parsed_fields = parse_object_input(fields, name='fields')
    params = {'entityTypeId': entity_type_id, 'fields': parsed_fields}
    if dry_run:
        return await _dry_run_preview(
            bitrix,
            'crm.item.add',
            params,
            commit_hint='Dry run only. Pass dry_run=false to create the item.',
        )
    payload = await bitrix.call_method_payload('crm.item.add', params)
    return payload if isinstance(payload, dict) else {'result': payload}


async def crm_item_update_data(
    bitrix: BitrixClient,
    entity: str | int,
    item_id: int,
    fields: dict[str, Any] | str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
        raise BitrixApiError('item_id must be a positive integer.')
    entity_type_id = resolve_crm_entity_type_id(entity)
    parsed_fields = parse_object_input(fields, name='fields')
    params = {'entityTypeId': entity_type_id, 'id': item_id, 'fields': parsed_fields}
    if dry_run:
        return await _dry_run_preview(
            bitrix,
            'crm.item.update',
            params,
            commit_hint='Dry run only. Pass dry_run=false to update the item.',
        )
    payload = await bitrix.call_method_payload('crm.item.update', params)
    return payload if isinstance(payload, dict) else {'result': payload}


async def task_add_data(
    bitrix: BitrixClient,
    fields: dict[str, Any] | str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    parsed_fields = parse_object_input(fields, name='fields')
    params = {'fields': parsed_fields}
    if dry_run:
        return await _dry_run_preview(
            bitrix,
            'tasks.task.add',
            params,
            commit_hint='Dry run only. Pass dry_run=false to create the task.',
        )
    payload = await bitrix.call_method_payload('tasks.task.add', params)
    return payload if isinstance(payload, dict) else {'result': payload}


async def task_update_data(
    bitrix: BitrixClient,
    task_id: int,
    fields: dict[str, Any] | str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        raise BitrixApiError('task_id must be a positive integer.')
    parsed_fields = parse_object_input(fields, name='fields')
    params = {'taskId': task_id, 'fields': parsed_fields}
    if dry_run:
        return await _dry_run_preview(
            bitrix,
            'tasks.task.update',
            params,
            commit_hint='Dry run only. Pass dry_run=false to update the task.',
        )
    payload = await bitrix.call_method_payload('tasks.task.update', params)
    return payload if isinstance(payload, dict) else {'result': payload}


async def activity_add_data(
    bitrix: BitrixClient,
    fields: dict[str, Any] | str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    parsed_fields = parse_object_input(fields, name='fields')
    params = {'fields': parsed_fields}
    if dry_run:
        return await _dry_run_preview(
            bitrix,
            'crm.activity.add',
            params,
            commit_hint='Dry run only. Pass dry_run=false to create the activity.',
        )
    payload = await bitrix.call_method_payload('crm.activity.add', params)
    return payload if isinstance(payload, dict) else {'result': payload}
