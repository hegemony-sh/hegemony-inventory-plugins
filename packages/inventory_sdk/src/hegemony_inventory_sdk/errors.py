# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed inventory provider errors and safe response envelopes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import ProviderErrorCode


@dataclass(slots=True)
class ProviderErrorEnvelope:
    """Safe provider error payload returned by APIs and resolver paths."""

    provider_id: str
    provider_type: str
    operation: str
    code: ProviderErrorCode
    message: str
    retryable: bool = False
    resource_type: str | None = None
    cause_chain: list[str] = field(default_factory=list)
    safe_details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_type": self.provider_type,
            "operation": self.operation,
            "resource_type": self.resource_type,
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "cause_chain": self.cause_chain,
            "safe_details": self.safe_details,
        }


class InventoryProviderError(Exception):
    """Exception carrying a safe provider error envelope."""

    def __init__(self, envelope: ProviderErrorEnvelope):
        super().__init__(envelope.message)
        self.envelope = envelope

    @classmethod
    def from_exception(
        cls,
        *,
        provider_id: str,
        provider_type: str,
        operation: str,
        code: ProviderErrorCode,
        message: str,
        exc: BaseException | None = None,
        retryable: bool = False,
        resource_type: str | None = None,
        safe_details: dict[str, Any] | None = None,
    ) -> InventoryProviderError:
        cause_chain: list[str] = []
        if exc is not None:
            cause_chain.append(type(exc).__name__)
        return cls(
            ProviderErrorEnvelope(
                provider_id=provider_id,
                provider_type=provider_type,
                operation=operation,
                code=code,
                message=message,
                retryable=retryable,
                resource_type=resource_type,
                cause_chain=cause_chain,
                safe_details=safe_details or {},
            )
        )


def not_supported(provider_id: str, provider_type: str, operation: str) -> InventoryProviderError:
    return InventoryProviderError(
        ProviderErrorEnvelope(
            provider_id=provider_id,
            provider_type=provider_type,
            operation=operation,
            code=ProviderErrorCode.MALFORMED_QUERY,
            message=f"Inventory provider operation '{operation}' is not supported",
            retryable=False,
        )
    )
