"""Structured logging with request and tenant context (design §12, N-11).

N-11: "structured logging and error tracking in production."

The point of the tenant identifier in every line is practical: when a customer
reports that a gate pass would not release, the first question is *which
customer*, and grepping a shared log without that field is guesswork. The
request id then ties every line from one request together.

Nothing here logs a secret or a password — see ``core.audit.REDACTED_FIELDS``
for the same rule applied to the audit trail.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

#: Correlates every log line emitted while handling one request.
_request_id: ContextVar[str | None] = ContextVar("yardflow_request_id", default=None)


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str | None):  # type: ignore[no-untyped-def]
    return _request_id.set(value)


class TenantContextFilter(logging.Filter):
    """Attach ``request_id`` and ``organization_id`` to every record.

    A filter rather than a formatter, so the fields are available to any handler
    and any format — JSON in production, plain text locally.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        from core.tenancy import get_current_organization_id

        record.request_id = get_request_id() or "-"
        organization_id = get_current_organization_id()
        record.organization_id = str(organization_id) if organization_id else "-"
        return True


class RequestIdMiddleware:
    """Assign a request id and expose it on the response.

    Honours an inbound ``X-Request-ID`` so a load balancer or client trace id is
    preserved rather than replaced (§12.1).
    """

    header = "HTTP_X_REQUEST_ID"
    response_header = "X-Request-ID"

    def __init__(self, get_response):  # type: ignore[no-untyped-def]
        self.get_response = get_response

    def __call__(self, request):  # type: ignore[no-untyped-def]
        incoming = request.META.get(self.header)
        request_id = (incoming or uuid.uuid4().hex)[:64]
        request.request_id = request_id

        token = set_request_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)

        response[self.response_header] = request_id
        return response


def configure_error_tracking() -> bool:
    """Wire error tracking if it is configured and available (N-11).

    Deliberately optional and silent when absent: local development needs no
    error tracker, and a missing DSN must not stop the application booting. The
    tenant and request ids above are attached as tags, so an error report says
    which customer it belongs to.
    """
    from django.conf import settings

    dsn = getattr(settings, "SENTRY_DSN", "") or ""
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.django import DjangoIntegration
    except ImportError:
        logging.getLogger(__name__).warning(
            "SENTRY_DSN is set but sentry-sdk is not installed; error tracking is off."
        )
        return False

    sentry_sdk.init(
        dsn=dsn,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        environment=getattr(settings, "ENVIRONMENT", "production"),
        # Never send request bodies or headers: they carry credentials and
        # customer data (N-4).
        send_default_pii=False,
        traces_sample_rate=float(getattr(settings, "SENTRY_TRACES_SAMPLE_RATE", 0.0)),
    )

    def _tag_scope(event, hint):  # type: ignore[no-untyped-def]
        from core.tenancy import get_current_organization_id

        organization_id = get_current_organization_id()
        event.setdefault("tags", {})
        event["tags"]["organization_id"] = str(organization_id) if organization_id else "-"
        event["tags"]["request_id"] = get_request_id() or "-"
        return event

    sentry_sdk.Hub.current.client.options["before_send"] = _tag_scope  # type: ignore[union-attr]
    return True
