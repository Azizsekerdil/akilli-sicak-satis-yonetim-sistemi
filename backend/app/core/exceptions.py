"""
Domain exception hierarchy.

Every exception carries an **i18n key** rather than a rendered sentence, so the
same error can be shown in Turkish or English depending on the caller's
language.  The API layer turns these into structured JSON responses.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all handled application errors."""

    status_code: int = 400
    error_code: str = "app_error"

    def __init__(
        self,
        message_key: str | None = None,
        *,
        params: dict[str, Any] | None = None,
        detail: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message_key = message_key or f"error.{self.error_code}"
        self.params = params or {}
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code
        super().__init__(detail or self.message_key)

    def to_dict(self, rendered: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": self.error_code,
            "message_key": self.message_key,
            "message": rendered or self.detail or self.message_key,
        }
        if self.params:
            body["params"] = self.params
        if self.detail and rendered:
            body["detail"] = self.detail
        return body


# --- 4xx -------------------------------------------------------------------
class ValidationError(AppError):
    status_code = 422
    error_code = "validation_error"


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"


class ConflictError(AppError):
    status_code = 409
    error_code = "conflict"


class AuthenticationError(AppError):
    status_code = 401
    error_code = "authentication_failed"


class PermissionDeniedError(AppError):
    status_code = 403
    error_code = "permission_denied"


class RateLimitError(AppError):
    status_code = 429
    error_code = "rate_limited"


# --- Business rules --------------------------------------------------------
class BusinessRuleError(AppError):
    status_code = 400
    error_code = "business_rule_violation"


class InsufficientStockError(BusinessRuleError):
    error_code = "insufficient_stock"


class CreditLimitExceededError(BusinessRuleError):
    error_code = "credit_limit_exceeded"


class ExpiredLotError(BusinessRuleError):
    error_code = "expired_lot"


class DaySessionError(BusinessRuleError):
    error_code = "day_session_error"


class ReconciliationError(BusinessRuleError):
    error_code = "reconciliation_error"


# --- Subsystems ------------------------------------------------------------
class AIProviderError(AppError):
    status_code = 502
    error_code = "ai_provider_error"


class AIBudgetExceededError(AppError):
    status_code = 402
    error_code = "ai_budget_exceeded"


class UnsafeQueryError(AppError):
    status_code = 400
    error_code = "unsafe_query"


class BackupError(AppError):
    status_code = 500
    error_code = "backup_error"


class RestoreError(AppError):
    status_code = 500
    error_code = "restore_error"


class OptimizationError(AppError):
    status_code = 500
    error_code = "optimization_error"


__all__ = [
    "AppError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "AuthenticationError",
    "PermissionDeniedError",
    "RateLimitError",
    "BusinessRuleError",
    "InsufficientStockError",
    "CreditLimitExceededError",
    "ExpiredLotError",
    "DaySessionError",
    "ReconciliationError",
    "AIProviderError",
    "AIBudgetExceededError",
    "UnsafeQueryError",
    "BackupError",
    "RestoreError",
    "OptimizationError",
]
