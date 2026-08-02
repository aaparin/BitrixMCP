from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bitrix_mcp.errors import BitrixApiError
from bitrix_mcp.oauth.identity import OAuthIdentity
from bitrix_mcp.oauth.resolver import identity_kind
from bitrix_mcp.oauth.store import TokenStore


@dataclass(frozen=True)
class OwnershipRule:
    method: str
    entity: str  # crm_item | task | activity
    id_params: tuple[str, ...]
    owner_fields: tuple[str, ...]
    fields_param: str = 'fields'
    entity_type_id_param: str | None = None
    static_entity_type_id: int | None = None


# entityTypeId for static CRM entities used by crm.*.update/add/delete
_STATIC_CRM = {
    'lead': 1,
    'deal': 2,
    'contact': 3,
    'company': 4,
    'quote': 7,
    'invoice': 31,
}


def _crm_item_rules() -> list[OwnershipRule]:
    rules: list[OwnershipRule] = [
        OwnershipRule(
            method='crm.item.update',
            entity='crm_item',
            id_params=('id', 'ID'),
            owner_fields=('assignedById', 'ASSIGNED_BY_ID'),
            entity_type_id_param='entityTypeId',
        ),
        OwnershipRule(
            method='crm.item.add',
            entity='crm_item',
            id_params=(),
            owner_fields=('assignedById', 'ASSIGNED_BY_ID'),
            entity_type_id_param='entityTypeId',
        ),
        OwnershipRule(
            method='crm.item.delete',
            entity='crm_item',
            id_params=('id', 'ID'),
            owner_fields=('assignedById', 'ASSIGNED_BY_ID'),
            entity_type_id_param='entityTypeId',
        ),
    ]
    for name, type_id in _STATIC_CRM.items():
        for action in ('update', 'delete'):
            rules.append(
                OwnershipRule(
                    method=f'crm.{name}.{action}',
                    entity='crm_item',
                    id_params=('id', 'ID'),
                    owner_fields=('assignedById', 'ASSIGNED_BY_ID'),
                    static_entity_type_id=type_id,
                )
            )
        rules.append(
            OwnershipRule(
                method=f'crm.{name}.add',
                entity='crm_item',
                id_params=(),
                owner_fields=('assignedById', 'ASSIGNED_BY_ID'),
                static_entity_type_id=type_id,
            )
        )
    return rules


def default_ownership_rules(*, task_owner_fields: tuple[str, ...] = ('RESPONSIBLE_ID',)) -> dict[str, OwnershipRule]:
    rules = _crm_item_rules()
    rules.extend(
        [
            OwnershipRule(
                method='tasks.task.update',
                entity='task',
                id_params=('taskId', 'TASK_ID', 'id', 'ID'),
                owner_fields=task_owner_fields,
            ),
            OwnershipRule(
                method='tasks.task.delete',
                entity='task',
                id_params=('taskId', 'TASK_ID', 'id', 'ID'),
                owner_fields=task_owner_fields,
            ),
            OwnershipRule(
                method='tasks.task.add',
                entity='task',
                id_params=(),
                owner_fields=task_owner_fields,
                fields_param='fields',
            ),
            OwnershipRule(
                method='crm.activity.update',
                entity='activity',
                id_params=('id', 'ID'),
                owner_fields=('RESPONSIBLE_ID', 'responsibleId'),
            ),
            OwnershipRule(
                method='crm.activity.delete',
                entity='activity',
                id_params=('id', 'ID'),
                owner_fields=('RESPONSIBLE_ID', 'responsibleId'),
            ),
            OwnershipRule(
                method='crm.activity.add',
                entity='activity',
                id_params=(),
                owner_fields=('RESPONSIBLE_ID', 'responsibleId'),
                fields_param='fields',
            ),
        ]
    )
    return {rule.method.lower(): rule for rule in rules}


@dataclass
class OwnershipDecision:
    allowed: bool
    result: str  # ok | denied | admin_bypass | no_rule | missing_id
    reason: str = ''
    owner_name: str | None = None
    mutated_params: dict[str, Any] | None = None


class OwnershipGuard:
    def __init__(
        self,
        *,
        rules: dict[str, OwnershipRule] | None = None,
        admin_emails: frozenset[str] = frozenset(),
        enabled: bool = True,
    ):
        self.rules = rules or default_ownership_rules()
        self.admin_emails = {TokenStore.normalize_email(email) for email in admin_emails}
        self.enabled = enabled

    async def check(self, bitrix, method: str, params: dict[str, Any], identity) -> OwnershipDecision:
        if not self.enabled:
            return OwnershipDecision(allowed=True, result='ok', mutated_params=dict(params))

        if identity_kind(identity) != 'oauth' or not isinstance(identity, OAuthIdentity):
            # Ownership applies only to per-user OAuth writes. Webhook / service
            # identities are gated by MethodPolicy (and BITRIX_ALLOW_WEBHOOK_WRITES).
            return OwnershipDecision(
                allowed=True,
                result='skipped',
                mutated_params=dict(params),
            )

        email = TokenStore.normalize_email(identity.email)
        if email in self.admin_emails:
            return OwnershipDecision(
                allowed=True,
                result='admin_bypass',
                mutated_params=dict(params),
            )

        rule = self.rules.get(method.strip().lower())
        if rule is None:
            return OwnershipDecision(
                allowed=False,
                result='no_rule',
                reason=f'Ownership cannot be verified for method "{method}".',
            )

        action = method.strip().lower().rsplit('.', 1)[-1]
        params = dict(params or {})

        if action == 'add':
            return await self._check_add(bitrix, rule, params, identity)
        return await self._check_mutate(bitrix, rule, params, identity)

    async def _check_add(self, bitrix, rule: OwnershipRule, params: dict[str, Any], identity: OAuthIdentity) -> OwnershipDecision:
        fields = params.get(rule.fields_param)
        if fields is None:
            fields = {}
            params[rule.fields_param] = fields
        if not isinstance(fields, dict):
            return OwnershipDecision(
                allowed=False,
                result='denied',
                reason=f'{rule.fields_param} must be an object.',
            )

        provided = None
        provided_key = None
        for key in rule.owner_fields:
            if key in fields:
                provided = fields[key]
                provided_key = key
                break
        if provided is not None and str(provided) != str(identity.bitrix_user_id):
            return OwnershipDecision(
                allowed=False,
                result='denied',
                reason='You cannot assign responsibility to another user.',
            )

        # Force responsible field.
        target_key = provided_key or rule.owner_fields[0]
        fields[target_key] = identity.bitrix_user_id
        # Clear conflicting aliases
        for key in rule.owner_fields:
            if key != target_key and key in fields:
                fields.pop(key, None)
        return OwnershipDecision(allowed=True, result='ok', mutated_params=params)

    async def _check_mutate(self, bitrix, rule: OwnershipRule, params: dict[str, Any], identity: OAuthIdentity) -> OwnershipDecision:
        record_id = None
        for key in rule.id_params:
            if key in params and params[key] not in (None, ''):
                record_id = params[key]
                break
        if record_id is None:
            return OwnershipDecision(
                allowed=False,
                result='missing_id',
                reason='Record id is required for ownership verification.',
            )

        record = await self._load_record(bitrix, rule, params, record_id)
        owner_value = None
        for key in rule.owner_fields:
            if key in record:
                owner_value = record.get(key)
                break
            # camelCase / nested item payloads
            lower_map = {str(k).lower(): k for k in record}
            alt = lower_map.get(key.lower())
            if alt is not None:
                owner_value = record.get(alt)
                break

        if owner_value is None or str(owner_value) != str(identity.bitrix_user_id):
            owner_name = None
            if owner_value not in (None, ''):
                try:
                    users = await bitrix.call_method(
                        'user.get',
                        {'filter': {'ID': int(owner_value)}, 'select': ['ID', 'NAME', 'LAST_NAME']},
                    )
                    if isinstance(users, list) and users:
                        owner_name = ' '.join(
                            part for part in [str(users[0].get('NAME') or ''), str(users[0].get('LAST_NAME') or '')] if part
                        ).strip() or None
                except Exception:
                    owner_name = None
            who = owner_name or 'another user'
            return OwnershipDecision(
                allowed=False,
                result='denied',
                reason=f'Only the responsible person can change this record (currently {who}).',
                owner_name=owner_name,
            )
        return OwnershipDecision(allowed=True, result='ok', mutated_params=params)

    async def _load_record(self, bitrix, rule: OwnershipRule, params: dict[str, Any], record_id: Any) -> dict[str, Any]:
        if rule.entity == 'crm_item':
            entity_type_id = rule.static_entity_type_id
            if entity_type_id is None and rule.entity_type_id_param:
                entity_type_id = params.get(rule.entity_type_id_param) or params.get('entityTypeId') or params.get('ENTITY_TYPE_ID')
            if entity_type_id is None:
                raise BitrixApiError('entityTypeId is required for CRM ownership verification.')
            payload = await bitrix.call_method_payload(
                'crm.item.get',
                {'entityTypeId': int(entity_type_id), 'id': int(record_id)},
            )
            result = payload.get('result') if isinstance(payload, dict) else payload
            if isinstance(result, dict) and isinstance(result.get('item'), dict):
                return result['item']
            if isinstance(result, dict):
                return result
            raise BitrixApiError('Unexpected crm.item.get response.')

        if rule.entity == 'task':
            payload = await bitrix.call_method_payload(
                'tasks.task.get',
                {
                    'taskId': int(record_id),
                    'select': ['ID', 'RESPONSIBLE_ID', 'CREATED_BY', 'TITLE'],
                },
            )
            result = payload.get('result') if isinstance(payload, dict) else payload
            if isinstance(result, dict) and isinstance(result.get('task'), dict):
                return result['task']
            if isinstance(result, dict):
                return result
            raise BitrixApiError('Unexpected tasks.task.get response.')

        if rule.entity == 'activity':
            payload = await bitrix.call_method_payload('crm.activity.get', {'id': int(record_id)})
            result = payload.get('result') if isinstance(payload, dict) else payload
            if isinstance(result, dict):
                return result
            raise BitrixApiError('Unexpected crm.activity.get response.')

        raise BitrixApiError(f'Unsupported ownership entity "{rule.entity}".')
