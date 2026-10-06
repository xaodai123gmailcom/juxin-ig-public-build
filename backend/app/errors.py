from __future__ import annotations


class DomainError(Exception):
    """Expected application error that can safely be returned to the UI."""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(DomainError):
    status_code = 422
    code = "validation_error"


class AuthenticationError(DomainError):
    status_code = 401
    code = "authentication_failed"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class InvalidTransitionError(ConflictError):
    code = "invalid_state_transition"


class UpstreamUnavailableError(DomainError):
    status_code = 503
    code = "upstream_unavailable"


class BitBrowserAuthRequiredError(UpstreamUnavailableError):
    """BitBrowser Local API is reachable, but its desktop session is signed out."""

    code = "auth_required"


class BitBrowserRateLimitedError(UpstreamUnavailableError):
    """BitBrowser has blocked Local API calls because list requests were too frequent."""

    code = "bitbrowser_rate_limited"
