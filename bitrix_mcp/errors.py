from __future__ import annotations


class BitrixApiError(RuntimeError):
    """Domain error for Bitrix24 REST / MCP tool failures."""


class MethodNotAllowed(BitrixApiError):
    """Raised when a REST method is blocked by MethodPolicy."""


# Back-compat alias for callers/tests that still import ReadOnlyViolation.
ReadOnlyViolation = MethodNotAllowed
