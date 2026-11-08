"""Errors of every adapter in one hierarchy; messages never carry tokens or webhook URLs."""


class CrmError(Exception):
    def __init__(self, message: str, *, crm: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(f"{crm}: {message}")
        self.crm = crm
        self.status = status
        self.code = code
        self.message = message


class CrmAuthError(CrmError):
    """The token or webhook is no longer accepted: the connection has to be authorised again."""


class CrmPermissionError(CrmError):
    """Authorised, but the token lacks a scope or the CRM plan lacks the feature."""


class CrmNotFound(CrmError):
    """The record does not exist (or was deleted)."""


class CrmValidationError(CrmError):
    """The CRM rejected the data."""


class CrmRateLimited(CrmError):
    """The CRM kept answering "too many requests" after every retry."""

    def __init__(self, message: str, *, crm: str, retry_after: float | None = None, **kw: object) -> None:
        super().__init__(message, crm=crm, **kw)  # type: ignore[arg-type]
        self.retry_after = retry_after


class CrmUnavailable(CrmError):
    """Network failure or a 5xx answer after every retry."""


class CrmUnsupported(CrmError):
    """The feature is not available in this CRM or in this CRM's configuration."""
