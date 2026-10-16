"""Exceptions shared by tools. Each maps to a structured tool result instead of a crash."""

from __future__ import annotations


class FinOpsError(Exception):
    """Base error. `remediation` tells the user how to fix it."""

    code = "error"

    def __init__(self, message: str, remediation: str | None = None):
        super().__init__(message)
        self.message = message
        self.remediation = remediation

    def to_dict(self) -> dict:
        d = {"status": self.code, "error": self.message}
        if self.remediation:
            d["remediation"] = self.remediation
        return d


class ReadOnlyViolation(FinOpsError):
    """Raised before sending any request that isn't on the read-only allowlist."""

    code = "blocked"


class ScopeError(FinOpsError):
    code = "out_of_scope"


class PermissionDenied(FinOpsError):
    code = "unavailable"

    def __init__(self, message: str, missing: str, remediation: str):
        super().__init__(message, remediation)
        self.missing = missing

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["missing"] = self.missing
        return d


class AzureApiError(FinOpsError):
    code = "azure_error"

    def __init__(self, message: str, status: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.request_id = request_id

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["http_status"] = self.status
        if self.request_id:
            d["request_id"] = self.request_id
        return d


class ValidationFailed(FinOpsError):
    code = "invalid"
