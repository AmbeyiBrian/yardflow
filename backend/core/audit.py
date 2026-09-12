"""Recording the audit trail (design §4.2, requirements M3, B6).

One function does the writing, so every event has the same shape and nothing
that should be recorded gets recorded differently somewhere else.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import models

from core.models import AuditLog, AuthMethod
from core.tenancy import get_current_organization_id, tenant_context

logger = logging.getLogger(__name__)

#: Never copied into ``before``/``after``. An audit trail that records password
#: hashes or tokens turns the evidence into a liability.
REDACTED_FIELDS = frozenset(
    {
        "password",
        "token",
        "refresh",
        "access",
        "secret",
        "public_key",
        "credential_id",
        "api_key",
    }
)


def snapshot(instance: models.Model, fields: list[str] | None = None) -> dict[str, Any]:
    """Capture a model's values for ``before``/``after`` (M3).

    Sensitive fields are redacted rather than omitted, so the trail still shows
    *that* something changed without recording the value.
    """
    data: dict[str, Any] = {}
    for field in instance._meta.concrete_fields:  # type: ignore[attr-defined]
        if fields is not None and field.name not in fields:
            continue
        if field.name in REDACTED_FIELDS:
            data[field.name] = "[redacted]"
            continue

        value = getattr(instance, field.attname, None)
        if value is None or isinstance(value, (str, int, float, bool)):
            data[field.name] = value
        else:
            data[field.name] = str(value)
    return data


def changed_fields(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Return only what actually differs.

    An audit entry listing forty unchanged fields buries the one that changed,
    which makes the trail harder to answer questions from — the opposite of the
    point (M3).
    """
    return {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }


def record(
    action: str,
    *,
    actor=None,
    organization=None,
    target: models.Model | None = None,
    target_label: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request=None,
    auth_method: str = "",
    note: str = "",
    actor_identifier: str = "",
) -> AuditLog | None:
    """Write one audit entry.

    Returns ``None`` when the event cannot be attributed to an organization. A
    tenant's audit trail must never contain another tenant's events, so an
    unattributable event — a failed login against an unknown subdomain, say —
    goes to the structured application log instead (N-11).
    """
    organization_id = getattr(organization, "pk", organization) or get_current_organization_id()

    ip = None
    user_agent = ""
    if request is not None:
        ip = client_ip(request)
        user_agent = (request.META.get("HTTP_USER_AGENT") or "")[:400]

    if actor is not None and not actor_identifier:
        actor_identifier = str(getattr(actor, "email", "") or getattr(actor, "phone", "") or "")

    if organization_id is None:
        logger.warning(
            "audit event outside any organization",
            extra={
                "action": action,
                "actor": actor_identifier,
                "ip": ip,
                "target": target_label,
                "note": note,
            },
        )
        return None

    # `record` is called from places that have no tenant context of their own —
    # a password reset, a platform admin action, a signal handler. Establishing
    # it here means callers never have to, and the row still lands under the
    # right organization for both the manager and the RLS policy.
    with tenant_context(organization_id):
        return _write(
            organization_id=organization_id,
            actor=actor if (actor is not None and getattr(actor, "pk", None)) else None,
            actor_identifier=actor_identifier[:254],
            action=action,
            target=target,
            target_label=target_label,
            before=before,
            after=after,
            ip=ip,
            user_agent=user_agent,
            auth_method=auth_method or "",
            note=note,
        )


def _write(**fields) -> AuditLog:
    """Create the row, with the tenant context already established."""
    target = fields.pop("target", None)
    target_label = fields.pop("target_label", "") or (str(target) if target is not None else "")
    note = fields.pop("note", "") or ""
    return AuditLog.objects.create(
        target_type=target._meta.label if target is not None else "",
        target_id=str(target.pk) if target is not None and target.pk else "",
        target_label=target_label[:255],
        note=note[:500],
        **fields,
    )


def record_system(action: str, **kwargs) -> AuditLog | None:
    """Record an action with no human actor — a beat task, an auto-approval."""
    kwargs.setdefault("auth_method", AuthMethod.SYSTEM)
    return record(action, **kwargs)


def client_ip(request) -> str | None:
    """Best-effort client address.

    Behind the ALB the useful value is the first entry in
    ``X-Forwarded-For`` (§12.1); ``REMOTE_ADDR`` would be the load balancer.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.META.get("REMOTE_ADDR") or None
