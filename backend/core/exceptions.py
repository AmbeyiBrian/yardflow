"""Domain exceptions (design §6.1, §13).

Every domain failure carries a **stable machine-readable code**. The frontend
switches on ``code``, never on message text (§6.1) — so messages can be reworded
or translated (N-8) without breaking a client.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for a refusal that is part of the domain, not a bug.

    ``code`` is the contract; ``message`` is for a human; ``field_errors`` maps
    directly onto react-hook-form paths; ``details`` carries whatever the client
    needs to render a useful message itself.
    """

    #: Stable identifier the client switches on. Never reword these.
    code: str = "DOMAIN_ERROR"

    #: 400 for "you asked for something invalid", 409 for "the world changed
    #: under you" (§13).
    status_code: int = 400

    default_message = "The request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        field_errors: dict[str, list[str]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.field_errors = field_errors or {}
        self.details = details or {}
        super().__init__(self.message)

    def to_envelope(self) -> dict[str, Any]:
        """Render the §6.1 error envelope."""
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field_errors:
            error["field_errors"] = self.field_errors
        if self.details:
            error["details"] = self.details
        return {"error": error}


# --------------------------------------------------------------------------
# Tenancy and access (§13)
# --------------------------------------------------------------------------


class OrganizationSuspended(DomainError):
    """A2: a suspended tenant may read, but may not post any movement."""

    code = "ORGANIZATION_SUSPENDED"
    status_code = 403
    default_message = (
        "This organization is suspended. Records remain available to read, but no "
        "new movements can be posted. Please contact support."
    )


class PermissionDeniedError(DomainError):
    """B4: the caller's roles do not carry the required permission."""

    code = "PERMISSION_DENIED"
    status_code = 403
    default_message = "You do not have permission to perform this action."


# --------------------------------------------------------------------------
# Document lifecycle (§13)
# --------------------------------------------------------------------------


class AlreadyPosted(DomainError):
    """M4: a posted document is immutable. Corrections are reversals."""

    code = "ALREADY_POSTED"
    status_code = 409
    default_message = (
        "This document has already been posted. Posted documents cannot be "
        "changed; correct it with a reversal instead."
    )


class InvalidTransition(DomainError):
    """§4.7: the status machine refuses this move."""

    code = "INVALID_TRANSITION"
    status_code = 409
    default_message = "That is not a valid change of status for this document."


class DocumentAmendmentNotAllowed(DomainError):
    """M4: amendment of posted documents is off unless a tenant enables it."""

    code = "AMENDMENT_NOT_ALLOWED"
    status_code = 403
    default_message = (
        "Amending posted documents is not enabled for this organization. Correct "
        "the document by reversal and re-entry."
    )
