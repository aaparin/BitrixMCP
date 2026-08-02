from __future__ import annotations

from typing import Any

from bitrix_mcp.methods.availability import AvailabilityProvider, PortalMethods
from bitrix_mcp.methods.catalog import MethodCatalog
from bitrix_mcp.methods.policy import MethodPolicy
from bitrix_mcp.methods.spec import AccessLevel, MethodSpec


async def search_methods_data(
    catalog: MethodCatalog,
    *,
    query: str = '',
    scope: str | None = None,
    access: AccessLevel | None = None,
    include_deprecated: bool = True,
    limit: int = 50,
    availability: AvailabilityProvider | PortalMethods | None = None,
) -> dict[str, Any]:
    specs = catalog.search(
        query,
        scope=scope,
        access=access,
        include_deprecated=include_deprecated,
        limit=limit,
    )
    portal = await _resolve_portal(availability)
    return {
        'query': query,
        'scope': scope,
        'access': access,
        'count': len(specs),
        'methods': [_enrich_spec(spec, portal) for spec in specs],
    }


async def describe_method_data(
    catalog: MethodCatalog,
    method: str,
    *,
    policy: MethodPolicy | None = None,
    availability: AvailabilityProvider | PortalMethods | None = None,
) -> dict[str, Any]:
    spec = catalog.get(method)
    portal = await _resolve_portal(availability)
    if spec is None:
        return {
            'found': False,
            'method': method,
            'suggestions': catalog.suggest(method),
        }

    payload = _enrich_spec(spec, portal)
    payload['found'] = True
    payload['accessSource'] = spec.access_source
    payload['source'] = spec.source
    if policy is not None:
        decision = policy.decide(method, {})
        payload['allowed'] = decision.allowed
        payload['policyReason'] = decision.reason or None
        if decision.warnings:
            payload['warnings'] = list(decision.warnings)
    return payload


async def list_scopes_data(
    catalog: MethodCatalog,
    *,
    availability: AvailabilityProvider | PortalMethods | None = None,
) -> dict[str, Any]:
    portal = await _resolve_portal(availability)
    catalog_scopes = catalog.scopes()
    methods = catalog.all_methods()

    available_count = None
    undocumented_available = None
    if portal is not None and not (portal.error and not portal.granted_methods):
        granted = {item.lower() for item in portal.granted_methods}
        documented = {spec.method.lower() for spec in methods}
        available_count = len(granted)
        undocumented_available = len(granted - documented)

    return {
        'catalogScopeCount': len(catalog_scopes),
        'catalogScopes': catalog_scopes,
        'catalogMethodCount': len(methods),
        'grantedScopes': sorted(portal.granted_scopes) if portal else [],
        'availableMethodCount': available_count,
        'undocumentedAvailableCount': undocumented_available,
        'portal': portal.to_public_dict() if portal else None,
    }


async def _resolve_portal(
    availability: AvailabilityProvider | PortalMethods | None,
) -> PortalMethods | None:
    if availability is None:
        return None
    if isinstance(availability, PortalMethods):
        return availability
    return await availability.get()


def _enrich_spec(spec: MethodSpec, portal: PortalMethods | None) -> dict[str, Any]:
    payload = spec.to_public_dict()
    if portal is None:
        payload['available'] = None
    else:
        payload['available'] = portal.is_available(spec.method)
    return payload
