"""API base classes and the error envelope (design §2.4, §6, §6.1).

Two things live here:

* :func:`exception_handler` — renders every failure in the one envelope shape
  described in §6.1, so the frontend has a single error contract
* :class:`TenantScopedViewSet` — the base for every tenant-facing viewset,
  returning **404 and never 403** for another tenant's objects (A3)
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.http import Http404
from rest_framework import exceptions as drf_exceptions
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import set_rollback

from accounts.api_permissions import HasPermission
from core.api_permissions import OrganizationIsActive
from core.exceptions import DomainError
from core.tenancy import TenantContextMissing

# Codes for failures that are not domain errors but still need a stable
# identifier in the envelope.
CODE_NOT_FOUND = "NOT_FOUND"
CODE_VALIDATION = "VALIDATION_ERROR"
CODE_PERMISSION_DENIED = "PERMISSION_DENIED"
CODE_NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
CODE_METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
CODE_THROTTLED = "THROTTLED"
CODE_PARSE_ERROR = "PARSE_ERROR"
CODE_SERVER_ERROR = "SERVER_ERROR"

_DRF_CODES: dict[type, str] = {
    drf_exceptions.NotAuthenticated: CODE_NOT_AUTHENTICATED,
    drf_exceptions.AuthenticationFailed: CODE_NOT_AUTHENTICATED,
    drf_exceptions.PermissionDenied: CODE_PERMISSION_DENIED,
    drf_exceptions.NotFound: CODE_NOT_FOUND,
    drf_exceptions.MethodNotAllowed: CODE_METHOD_NOT_ALLOWED,
    drf_exceptions.Throttled: CODE_THROTTLED,
    drf_exceptions.ParseError: CODE_PARSE_ERROR,
}


def flatten_field_errors(detail: Any, prefix: str = "") -> dict[str, list[str]]:
    """Flatten DRF's nested error detail into dotted paths.

    ``{"lines": [{}, {"length": ["Too long."]}]}`` becomes
    ``{"lines.1.length": ["Too long."]}`` — which is exactly the path
    react-hook-form uses, so the frontend can attach the message to the right
    input without transforming anything (§6.1).
    """
    errors: dict[str, list[str]] = {}

    if isinstance(detail, dict):
        for key, value in detail.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            errors.update(flatten_field_errors(value, path))
        return errors

    if isinstance(detail, list):
        # A list of plain messages is the error list for `prefix` itself; a list
        # containing structures is a collection of nested objects.
        if all(not isinstance(item, (dict, list)) for item in detail):
            if prefix:
                errors[prefix] = [str(item) for item in detail]
            return errors
        for index, item in enumerate(detail):
            path = f"{prefix}.{index}" if prefix else str(index)
            errors.update(flatten_field_errors(item, path))
        return errors

    if prefix:
        errors[prefix] = [str(detail)]
    return errors


def exception_handler(exc: Exception, context: dict) -> Response | None:
    """Render every failure in the §6.1 envelope.

    Ordering matters here. ``TenantContextMissing`` is deliberately translated
    into a 404 rather than a 500: reaching it through an API request means the
    caller addressed a tenant they have no access to, and saying anything more
    specific would confirm the resource exists (A3, §2.4).
    """
    if isinstance(exc, DomainError):
        set_rollback()
        return Response(exc.to_envelope(), status=exc.status_code)

    # Cross-tenant access and a missing tenant context both mean "not yours".
    if isinstance(exc, (Http404, TenantContextMissing)):
        set_rollback()
        return Response(
            {"error": {"code": CODE_NOT_FOUND, "message": "Not found."}},
            status=status.HTTP_404_NOT_FOUND,
        )

    if isinstance(exc, PermissionDenied):
        set_rollback()
        return Response(
            {
                "error": {
                    "code": CODE_PERMISSION_DENIED,
                    "message": str(exc) or "You do not have permission to perform this action.",
                }
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    if isinstance(exc, DjangoValidationError):
        set_rollback()
        field_errors = flatten_field_errors(getattr(exc, "message_dict", {}))
        return Response(
            {
                "error": {
                    "code": CODE_VALIDATION,
                    "message": "The submitted data is not valid.",
                    **({"field_errors": field_errors} if field_errors else {}),
                }
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, drf_exceptions.ValidationError):
        set_rollback()
        field_errors = flatten_field_errors(exc.detail)
        error: dict[str, Any] = {
            "code": CODE_VALIDATION,
            "message": _validation_message(field_errors),
        }
        if field_errors:
            error["field_errors"] = field_errors
        return Response({"error": error}, status=exc.status_code)

    if isinstance(exc, drf_exceptions.APIException):
        set_rollback()
        code = _DRF_CODES.get(type(exc), CODE_SERVER_ERROR)
        # Fall back to a code derived from the exception's own default_code.
        if code == CODE_SERVER_ERROR:
            for klass, mapped in _DRF_CODES.items():
                if isinstance(exc, klass):
                    code = mapped
                    break
        error = {"code": code, "message": _message_of(exc)}
        headers = {}
        if isinstance(exc, drf_exceptions.Throttled) and exc.wait:
            headers["Retry-After"] = str(int(exc.wait))
        return Response({"error": error}, status=exc.status_code, headers=headers or None)

    # Anything else is a bug. Let Django's handler log it and return a 500
    # rather than dressing it up as a domain error.
    return None


def _validation_message(field_errors: dict[str, list[str]]) -> str:
    """Say what is actually wrong, when there is one thing.

    "The submitted data is not valid." was the message on every refusal,
    whatever the reason — so a delegation missing its role and a delegation to
    yourself read identically, and the sentence explaining either sat unread in
    `field_errors`. A banner that never varies teaches people to ignore it.

    With a single problem the banner says it. With several, the generic line
    stands and the fields carry the detail — restating six messages in a banner
    is worse than pointing at the six fields.
    """
    messages = [message for messages in field_errors.values() for message in messages]

    # Errors that belong to no field have nowhere else to appear, so they win.
    for key in ("non_field_errors", "detail"):
        if field_errors.get(key):
            return field_errors[key][0]

    if len(messages) == 1:
        return messages[0]
    return "The submitted data is not valid."


def _message_of(exc: drf_exceptions.APIException) -> str:
    detail = exc.detail
    if isinstance(detail, list) and detail:
        return str(detail[0])
    if isinstance(detail, dict):
        first = next(iter(detail.values()), "")
        if isinstance(first, list) and first:
            return str(first[0])
        return str(first)
    return str(detail)


class TenantScopedViewSet(viewsets.ModelViewSet):
    """Base viewset for tenant-owned resources (§2.4, layer 4 of four).

    Scoping is inherited rather than re-implemented: ``queryset`` uses the
    model's default manager, which is already filtered to the organization in
    context (§2.1). A lookup for another tenant's id therefore misses, and
    ``get_object`` raises ``Http404`` — **404, never 403**, so an id is never
    confirmed to an outsider.

    Subclasses must not override ``get_queryset`` to use ``all_objects``. CI
    fails the build if they do (T1.20).

    Permissions: declare ``required_permission`` for the whole viewset, or
    ``required_permissions`` as an ``{action: codename}`` map when reading and
    writing need different rights — which is the common case, since anyone
    raising a gate-out needs to *read* the catalogue while only an admin may
    change it (B4, C3).
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive, HasPermission]

    #: The model this viewset exposes. Set this rather than ``queryset``: a
    #: class-level ``Model.objects.all()`` is evaluated at import time, when no
    #: organization is in context, and the tenant manager rightly refuses. The
    #: queryset is therefore built per request, which is also when the tenant is
    #: actually known (§2.1).
    model: type[models.Model] | None = None

    #: Passed to ``select_related`` / ``prefetch_related`` on every query.
    select_related: tuple[str, ...] = ()
    prefetch_related: tuple[str, ...] = ()

    def __init_subclass__(cls, **kwargs):  # type: ignore[no-untyped-def]
        """Give DRF and the schema generator an empty queryset to introspect.

        Both want to read `queryset` at class level — for basename inference and
        for OpenAPI generation — and neither has a request, so neither has a
        tenant. `_base_manager.none()` names the model while reaching no rows at
        all, which is exactly what they need and nothing more. Real requests go
        through `get_queryset()` below, which is scoped.
        """
        super().__init_subclass__(**kwargs)
        if cls.model is not None and getattr(cls, "queryset", None) is None:
            cls.queryset = cls.model._base_manager.none()

    def get_queryset(self):  # type: ignore[no-untyped-def]
        if self.model is None:
            raise NotImplementedError(
                f"{type(self).__name__} must set `model` (or override "
                f"`get_queryset`), so the queryset is built inside a request "
                f"where the tenant is known."
            )
        queryset = self.model.objects.all()
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.prefetch_related:
            queryset = queryset.prefetch_related(*self.prefetch_related)
        return queryset

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        # The organization is stamped by TenantModel.save() from context, so it
        # is never accepted from the request body — a client cannot choose which
        # tenant to write into.
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):  # type: ignore[no-untyped-def]
        serializer.save()
