from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from bitrix_mcp.errors import MethodNotAllowed, ReadOnlyViolation
from bitrix_mcp.methods.catalog import MethodCatalog, normalize_method_name
from bitrix_mcp.methods.spec import AccessLevel, MethodSpec
from bitrix_mcp.oauth.resolver import identity_kind

__all__ = [
    'MethodNotAllowed',
    'MethodPolicy',
    'PolicyDecision',
    'ReadOnlyViolation',
]


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    method: str
    access: AccessLevel
    reason: str = ''
    warnings: tuple[str, ...] = ()
    spec: MethodSpec | None = None
    identity_kind: str = 'webhook'
    requires_authorization: bool = False

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise MethodNotAllowed(self.reason)


@dataclass
class MethodPolicy:
    catalog: MethodCatalog
    allowed_access: frozenset[str] = field(default_factory=lambda: frozenset({'read'}))
    allow_unknown_methods: bool = False
    allow_webhook_writes: bool = False
    # When False, webhook writes follow BITRIX_ALLOWED_ACCESS only (legacy behaviour).
    oauth_enabled: bool = False

    def decide(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        identity: Any = None,
    ) -> PolicyDecision:
        method_name = method.strip()
        kind = identity_kind(identity)
        if not method_name:
            return PolicyDecision(
                allowed=False,
                method=method_name,
                access='unknown',
                reason='Bitrix24 method name is empty.',
                identity_kind=kind,
            )

        normalized = normalize_method_name(method_name)
        if normalized == 'batch':
            return self._decide_batch(method_name, params or {}, identity_kind=kind)

        spec = self.catalog.get(method_name)
        return self._decide_spec(method_name, spec, identity_kind=kind)

    def _decide_spec(
        self,
        method_name: str,
        spec: MethodSpec | None,
        *,
        identity_kind: str,
    ) -> PolicyDecision:
        warnings: list[str] = []
        if spec is None:
            access: AccessLevel = 'unknown'
            if self.allow_unknown_methods and 'read' in self.allowed_access:
                return self._apply_identity_gate(
                    PolicyDecision(
                        allowed=True,
                        method=method_name,
                        access='read',
                        warnings=('unknown method treated as read because BITRIX_ALLOW_UNKNOWN_METHODS=true',),
                        identity_kind=identity_kind,
                    )
                )
            return PolicyDecision(
                allowed=False,
                method=method_name,
                access=access,
                reason=self._deny_message(method_name, access, missing=True),
                warnings=tuple(warnings),
                identity_kind=identity_kind,
            )

        access = spec.access
        if spec.deprecated:
            replacement = f'; use {spec.replaced_by}' if spec.replaced_by else ''
            warnings.append(f'deprecated{replacement}')

        if access == 'unknown':
            if self.allow_unknown_methods and 'read' in self.allowed_access:
                return self._apply_identity_gate(
                    PolicyDecision(
                        allowed=True,
                        method=method_name,
                        access=access,
                        warnings=tuple(warnings)
                        + ('unknown access treated as read because BITRIX_ALLOW_UNKNOWN_METHODS=true',),
                        spec=spec,
                        identity_kind=identity_kind,
                    )
                )
            return PolicyDecision(
                allowed=False,
                method=method_name,
                access=access,
                reason=self._deny_message(method_name, access),
                warnings=tuple(warnings),
                spec=spec,
                identity_kind=identity_kind,
            )

        if access not in self.allowed_access:
            return PolicyDecision(
                allowed=False,
                method=method_name,
                access=access,
                reason=self._deny_message(method_name, access),
                warnings=tuple(warnings),
                spec=spec,
                identity_kind=identity_kind,
            )

        return self._apply_identity_gate(
            PolicyDecision(
                allowed=True,
                method=method_name,
                access=access,
                warnings=tuple(warnings),
                spec=spec,
                identity_kind=identity_kind,
            )
        )

    def _apply_identity_gate(self, decision: PolicyDecision) -> PolicyDecision:
        if not decision.allowed:
            return decision
        if decision.access not in {'write', 'destructive'}:
            return decision
        if not self.oauth_enabled:
            return decision
        if decision.identity_kind == 'oauth':
            return decision
        if self.allow_webhook_writes:
            return decision
        return PolicyDecision(
            allowed=False,
            method=decision.method,
            access=decision.access,
            reason=(
                f'Method "{decision.method}" requires a user OAuth identity. '
                'Webhook writes are disabled. Authorize with bitrix_authorize or open the provided link.'
            ),
            warnings=decision.warnings,
            spec=decision.spec,
            identity_kind=decision.identity_kind,
            requires_authorization=True,
        )

    def _decide_batch(
        self,
        method_name: str,
        params: dict[str, Any],
        *,
        identity_kind: str,
    ) -> PolicyDecision:
        commands = params.get('cmd')
        if commands is None:
            return PolicyDecision(
                allowed=False,
                method=method_name,
                access='unknown',
                reason='batch requires params.cmd as an object or array of subcommands.',
                identity_kind=identity_kind,
            )

        parsed = self._parse_batch_methods(commands)
        if not parsed.ok:
            return PolicyDecision(
                allowed=False,
                method=method_name,
                access='unknown',
                reason=parsed.reason,
                identity_kind=identity_kind,
            )
        if not parsed.methods:
            return PolicyDecision(
                allowed=False,
                method=method_name,
                access='unknown',
                reason='batch params.cmd did not contain any subcommands.',
                identity_kind=identity_kind,
            )

        warnings: list[str] = []
        for submethod in parsed.methods:
            if normalize_method_name(submethod) == 'batch':
                return PolicyDecision(
                    allowed=False,
                    method=method_name,
                    access='write',
                    reason='Nested batch is not allowed.',
                    identity_kind=identity_kind,
                )
            decision = self._decide_spec(submethod, self.catalog.get(submethod), identity_kind=identity_kind)
            warnings.extend(decision.warnings)
            if decision.access in {'write', 'destructive'} or decision.requires_authorization:
                return PolicyDecision(
                    allowed=False,
                    method=method_name,
                    access=decision.access if decision.access != 'read' else 'write',
                    reason=(
                        f'batch blocked because subcommand "{submethod}" is a write/destructive operation. '
                        'Batch remains read-only under the ownership policy.'
                    ),
                    warnings=tuple(warnings),
                    identity_kind=identity_kind,
                )
            if not decision.allowed:
                return PolicyDecision(
                    allowed=False,
                    method=method_name,
                    access=decision.access,
                    reason=f'batch blocked because subcommand "{submethod}" is not allowed: {decision.reason}',
                    warnings=tuple(warnings),
                    identity_kind=identity_kind,
                )

        return PolicyDecision(
            allowed=True,
            method=method_name,
            access='read',
            warnings=tuple(warnings),
            spec=self.catalog.get(method_name),
            identity_kind=identity_kind,
        )

    def _parse_batch_methods(self, commands: Any) -> '_BatchParseResult':
        if isinstance(commands, dict):
            values = list(commands.values())
        elif isinstance(commands, list):
            values = list(commands)
        else:
            return _BatchParseResult(ok=False, reason='batch params.cmd must be an object or array.')

        methods: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                return _BatchParseResult(
                    ok=False,
                    reason='batch params.cmd entries must be non-empty strings.',
                )
            raw = unquote(value.strip())
            method = raw.split('?', 1)[0].strip()
            if not method:
                return _BatchParseResult(
                    ok=False,
                    reason='batch subcommand method name is empty.',
                )
            methods.append(method)
        return _BatchParseResult(ok=True, methods=methods)

    def _deny_message(self, method_name: str, access: AccessLevel, *, missing: bool = False) -> str:
        where = 'not found in the method catalog' if missing else f'classified as access="{access}"'
        allowed = ', '.join(sorted(self.allowed_access)) or '(none)'
        return (
            f'Method "{method_name}" is {where} and is blocked by current policy '
            f'(BITRIX_ALLOWED_ACCESS={allowed}). '
            'Use bitrix_search_methods / bitrix_describe_method to inspect access levels, '
            'or adjust BITRIX_ALLOWED_ACCESS / BITRIX_ALLOW_UNKNOWN_METHODS.'
        )


@dataclass(frozen=True)
class _BatchParseResult:
    ok: bool
    reason: str = ''
    methods: list[str] = field(default_factory=list)
