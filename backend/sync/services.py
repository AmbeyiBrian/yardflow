"""Applying queued offline mutations (design §8.2, §8.3, §8.4; N1–N3).

The three rules this module exists to enforce, in the order they matter:

**§8.3 — the approval hole stays closed.** "Release of an unapproved gate-out is
impossible offline." A queued release names a pass that must *already* be
approved; if it is not, the submission is refused. Not queued for approval, not
applied optimistically — refused. §8.3 calls this "the single most important
constraint in the offline design", and it is: without it, offline mode is a
bypass around the entire control the product exists to provide.

**§8.2 — a replay is not a second document.** The client's ``client_uuid`` is
unique per organization, so the second arrival of a payload finds the first
submission and returns its document with 200.

**§8.4 — a conflict is neither forced nor dropped.** The server revalidates
against *current* stock. A document that is no longer valid becomes a
``SyncException`` carrying the payload and the reason, and somebody resolves it.
Force-posting would corrupt a balance; dropping would lose the storekeeper's work
and their trust with it.

One thing deliberately not done here: nothing in this module writes movements
itself. Every submission goes through the same service a request would —
``post_gate_in``, ``submit_gate_out``, ``release_gate_out`` — because a second
posting path is a second set of rules, and the offline one would be the one
nobody tests against.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.exceptions import DomainError
from sync.models import (
    ExceptionStatus,
    SubmissionStatus,
    SyncException,
    SyncOperation,
    SyncSubmission,
)

logger = logging.getLogger(__name__)


class SyncRefused(DomainError):
    """The submission cannot be accepted at all, offline or otherwise."""

    code = "SYNC_REFUSED"
    status_code = 400
    default_message = "This cannot be submitted from an offline queue."


class OfflineApprovalAttempt(SyncRefused):
    """§8.3: the constraint that makes offline mode safe.

    A separate class because this refusal is not a validation quibble — it is the
    control. It gets its own code so a client can recognise it and say the right
    thing, and so it is greppable when somebody later asks whether the hole was
    ever closed.
    """

    code = "OFFLINE_APPROVAL_NOT_ALLOWED"
    status_code = 409
    default_message = (
        "Approval needs a connection. A pass can only be released offline if it "
        "was already approved and downloaded (N3, §8.3)."
    )


def apply_submission(
    *,
    organization,
    client_uuid,
    operation: str,
    payload: dict,
    submitted_by=None,
    captured_at=None,
    request=None,
) -> tuple[SyncSubmission, bool]:
    """Apply one queued mutation. Returns ``(submission, was_replay)``.

    The replay check comes first and outside the work: a retry after a timeout is
    the commonest thing that happens to this endpoint, and it must be cheap and
    certain rather than depending on an insert failing later.
    """
    existing = SyncSubmission.objects.filter(client_uuid=client_uuid).first()
    if existing is not None:
        # §8.2: the same uuid always resolves to the same outcome — including
        # when that outcome was a rejection, so a phone retrying a doomed
        # submission does not accumulate exceptions.
        return existing, True

    handler = _HANDLERS.get(operation)
    if handler is None:
        raise SyncRefused(
            f"{operation} cannot be captured offline. §8 keeps the queue to "
            f"gate-in, gate-out and recording money entries (§4.17.8) — never "
            f"approving or paying — because a queue that accepted everything "
            f"would be a second write path around every control.",
            details={"operation": operation, "allowed": sorted(_HANDLERS)},
        )

    submission = SyncSubmission(
        organization=organization,
        client_uuid=client_uuid,
        operation=operation,
        payload=payload,
        submitted_by=submitted_by,
        captured_at=captured_at,
    )

    try:
        with transaction.atomic():
            submission.save()
    except IntegrityError:
        # Two devices, or two threads, with the same uuid at once. The winner is
        # whoever inserted; this caller reads their result.
        existing = SyncSubmission.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return existing, True
        raise

    try:
        with transaction.atomic():
            document = handler(payload, submitted_by=submitted_by, request=request)
    except DomainError as refusal:
        # §8.4: not force-posted, not dropped. Recorded with everything needed to
        # resolve it — including the payload, which is the only record of what the
        # person on site actually said.
        _record_exception(submission, refusal)
        return submission, False

    submission.status = SubmissionStatus.APPLIED
    submission.document_type = document._meta.label
    submission.document_id = str(document.pk)
    submission.document_number = getattr(document, "number", "") or ""
    submission.applied_at = timezone.now()
    submission.save(
        update_fields=[
            "status",
            "document_type",
            "document_id",
            "document_number",
            "applied_at",
            "updated_at",
        ]
    )
    _resolve_superseded(submission, payload, submitted_by, request)
    return submission, False


def _record_exception(submission: SyncSubmission, refusal: DomainError) -> None:
    submission.status = SubmissionStatus.REJECTED
    submission.save(update_fields=["status", "updated_at"])

    SyncException.objects.create(
        organization_id=submission.organization_id,
        submission=submission,
        code=getattr(refusal, "code", "") or "",
        reason=str(refusal),
        details=getattr(refusal, "details", None) or {},
    )
    logger.info(
        "sync submission %s rejected: %s",
        submission.client_uuid,
        getattr(refusal, "code", "") or refusal,
    )


# --------------------------------------------------------------------------
# The handlers. Each calls the same service a request would.
# --------------------------------------------------------------------------


def _apply_gate_in(payload: dict, *, submitted_by=None, request=None):
    """A delivery captured at the gate with no signal (D17, N1)."""
    from receiving.services import post_gate_in
    from receiving.views import GateInSerializer

    serializer = GateInSerializer(data=payload)
    _validate(serializer)
    gate_in = serializer.save(created_by=submitted_by)

    # Captured means received: the storekeeper stood there and counted it. The
    # document posts immediately, exactly as it would have online.
    return post_gate_in(gate_in, posted_by=submitted_by, request=request)


def _apply_gate_out_request(payload: dict, *, submitted_by=None, request=None):
    """A request raised on site (F1, N1).

    Submitted for approval, never approved. An offline request that arrived
    already approved would be the hole §8.3 closes, from the other direction.
    """
    from dispatch.services import submit_gate_out
    from dispatch.views import GateOutSerializer

    serializer = GateOutSerializer(data=payload)
    _validate(serializer)
    gate_out = serializer.save(created_by=submitted_by, requested_by=submitted_by)

    return submit_gate_out(gate_out, submitted_by=submitted_by, request=request)


def _apply_gate_out_release(payload: dict, *, submitted_by=None, request=None):
    """Release a pass that was **already approved** before it went offline (§8.3, N3).

    Everything about this handler is that sentence. It refuses to create a pass,
    refuses to approve one, and refuses to release one that is not already
    approved — so the worst an offline device can do is release material somebody
    with authority had already authorised leaving.
    """
    from dispatch.models import RELEASABLE_STATUSES, GateOut
    from dispatch.services import release_gate_out

    gate_out_id = payload.get("gate_out")
    if not gate_out_id:
        raise SyncRefused(
            "An offline release names the pass it is releasing. It cannot create "
            "one (§8.3)."
        )

    gate_out = GateOut.objects.filter(pk=gate_out_id).first()
    if gate_out is None:
        # A pass from another tenant, or one that no longer exists. Either way
        # this device has no business releasing it (A3).
        raise SyncRefused(
            "That gate pass does not exist here.", details={"gate_out": gate_out_id}
        )

    if gate_out.status not in RELEASABLE_STATUSES:
        raise OfflineApprovalAttempt(
            f"{gate_out.number or 'That pass'} is "
            f"{gate_out.get_status_display().lower()}, not approved. Approval "
            f"needs a connection (N3, §8.3).",
            details={"gate_out": str(gate_out.pk), "status": gate_out.status},
        )

    return release_gate_out(
        gate_out,
        released_by=submitted_by,
        released_lines=payload.get("lines"),
        vehicle_reg=payload.get("vehicle_reg", ""),
        driver_name=payload.get("driver_name", ""),
        variance_reasons=payload.get("variance_reasons"),
        released_serials=payload.get("released_serials"),
        request=request,
    )


# --------------------------------------------------------------------------
# Money entries (R6, §4.17.8). The same services the online forms call;
# nothing here approves, pays or closes anything (§8.3).
# --------------------------------------------------------------------------


def _bad(field: str, message: str) -> SyncRefused:
    return SyncRefused(message, details={"field_errors": {field: message}})


def _uuid(payload: dict, key: str, *, required: bool = False):
    from uuid import UUID

    raw = payload.get(key)
    if raw in (None, ""):
        if required:
            raise _bad(key, "This is required.")
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        raise _bad(key, "Not a valid uuid.") from None


def _decimal(payload: dict, key: str, *, required: bool = False):
    from decimal import Decimal, InvalidOperation

    raw = payload.get(key)
    if raw in (None, ""):
        if required:
            raise _bad(key, "This is required.")
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        raise _bad(key, "Not a valid number.") from None


def _date(payload: dict, key: str):
    from datetime import date

    try:
        return date.fromisoformat(str(payload.get(key)))
    except ValueError:
        raise _bad(key, "Enter a valid date (YYYY-MM-DD).") from None


def _int_id(value, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _bad(field, "Not a valid id.") from None


def _tenant_object(model, payload: dict, key: str, *, required: bool = False):
    """The row ``payload[key]`` names, or ``None`` when absent.

    ``objects`` is the tenant-scoped manager, so another organization's id is
    simply not found, and is refused like a row that no longer exists (A3).
    """
    raw = payload.get(key)
    if raw in (None, ""):
        if required:
            raise _bad(key, "This is required.")
        return None
    found = model.objects.filter(pk=_int_id(raw, key)).first()
    if found is None:
        raise SyncRefused(
            f"The {key.replace('_', ' ')} you chose no longer exists here.",
            details={"field_errors": {key: "Not found."}},
        )
    return found


def _apply_expense(payload: dict, *, submitted_by=None, request=None):
    """An expense captured with no signal (R1, R6, §4.17.8)."""
    from commercials.finance import CasualLineInput, record_expense
    from commercials.models import AllowanceRequest, Casual, ExpenseCategory
    from jobs.models import Job
    from network.models import Project, Site

    category = _tenant_object(ExpenseCategory, payload, "category", required=True)
    lines = []
    for raw in payload.get("casual_lines") or []:
        if raw.get("casual") not in (None, ""):
            casual = Casual.objects.filter(pk=_int_id(raw["casual"], "casual_lines")).first()
        else:
            # Registered earlier in this same queue; the batch applies in capture
            # order, so it exists by now — or its registration was refused, which
            # is a clear reason to refuse this entry too.
            casual = Casual.objects.filter(
                client_uuid=_uuid(raw, "casual_client_uuid", required=True)
            ).first()
        if casual is None:
            raise SyncRefused(
                "A casual on this expense is not registered here. The "
                "registration may have been refused; fix that first.",
                details={"field_errors": {"casual_lines": "Casual not found."}},
            )
        lines.append(
            CasualLineInput(
                casual=casual,
                days=_int_id(raw.get("days"), "casual_lines"),
                amount=_decimal(raw, "amount"),
            )
        )

    return record_expense(
        actor=submitted_by,
        category=category,
        amount=_decimal(payload, "amount", required=True),
        incurred_on=_date(payload, "incurred_on"),
        site=_tenant_object(Site, payload, "site"),
        project=_tenant_object(Project, payload, "project"),
        job=_tenant_object(Job, payload, "job"),
        description=payload.get("description") or "",
        scope_of_work=payload.get("scope_of_work") or "",
        vehicle_reg=payload.get("vehicle_reg") or "",
        litres=_decimal(payload, "litres"),
        float_request=_tenant_object(AllowanceRequest, payload, "float_request"),
        photos_expected=_int_id(payload.get("photos_expected") or 0, "photos_expected"),
        casual_lines=lines,
        client_uuid=_uuid(payload, "client_uuid"),
        request=request,
    )


def _apply_allowance_request(payload: dict, *, submitted_by=None, request=None):
    """An allowance or float request raised offline (R2, R5, R6)."""
    from commercials.finance import request_allowance
    from network.models import Project, Site

    return request_allowance(
        actor=submitted_by,
        type=payload.get("type") or "",
        amount=_decimal(payload, "amount", required=True),
        from_date=_date(payload, "from_date"),
        to_date=_date(payload, "to_date"),
        site=_tenant_object(Site, payload, "site"),
        project=_tenant_object(Project, payload, "project"),
        reason=payload.get("reason") or "",
        transport_scope=payload.get("transport_scope") or "",
        client_uuid=_uuid(payload, "client_uuid"),
        request=request,
    )


def _apply_casual(payload: dict, *, submitted_by=None, request=None):
    """A casual registered on site (R3, R6)."""
    from commercials.finance import register_casual

    return register_casual(
        actor=submitted_by,
        name=str(payload.get("name") or ""),
        id_number=str(payload.get("id_number") or ""),
        phone=str(payload.get("phone") or ""),
        client_uuid=_uuid(payload, "client_uuid"),
        request=request,
    )


def _resolve_superseded(submission, payload: dict, resolved_by, request) -> None:
    """"Fix and resend" (R6): the corrected entry landing closes the old refusal.

    Only after a successful apply, so a correction that is itself refused leaves
    the original exception open. No exception to find (the old one applied, or
    never arrived) is fine.
    """
    old = payload.get("supersedes_client_uuid")
    if not old:
        return
    try:
        exception = SyncException.objects.filter(
            submission__client_uuid=old, status=ExceptionStatus.OPEN
        ).first()
    except (ValueError, DjangoValidationError):
        return
    if exception is None:
        return
    resolve_exception(
        exception, resolution="Corrected and resent", resolved_by=resolved_by, request=request
    )
    exception.replacement_submission = submission
    exception.save(update_fields=["replacement_submission", "updated_at"])


def _validate(serializer) -> None:
    """Turn a serializer failure into a domain refusal.

    So a payload that was valid on the phone but is not valid here — a site that
    was archived meanwhile, an item type that was renamed — becomes a resolvable
    exception rather than a 400 the queue would keep retrying (§8.4).
    """
    if serializer.is_valid():
        return
    raise SyncRefused(
        "This no longer validates against the current configuration.",
        details={"field_errors": _flatten(serializer.errors)},
    )


def _flatten(errors: Any) -> dict:
    from core.api import flatten_field_errors

    return flatten_field_errors(errors)


#: Keyed by the plain string value, not the enum member: the operation arrives
#: from JSON, and a dict keyed by the enum would miss on a str lookup under
#: strict typing even where it happens to work at runtime.
_HANDLERS: dict[str, Any] = {
    str(SyncOperation.GATE_IN): _apply_gate_in,
    str(SyncOperation.GATE_OUT_REQUEST): _apply_gate_out_request,
    str(SyncOperation.GATE_OUT_RELEASE): _apply_gate_out_release,
    str(SyncOperation.EXPENSE): _apply_expense,
    str(SyncOperation.ALLOWANCE_REQUEST): _apply_allowance_request,
    str(SyncOperation.CASUAL): _apply_casual,
}


# --------------------------------------------------------------------------
# Resolution (T8.7)
# --------------------------------------------------------------------------


class ExceptionNotOpen(DomainError):
    code = "SYNC_EXCEPTION_NOT_OPEN"
    status_code = 409
    default_message = "This sync exception has already been dealt with."


@transaction.atomic
def resolve_exception(
    exception: SyncException,
    *,
    resolution: str,
    discard: bool = False,
    resolved_by=None,
    request=None,
) -> SyncException:
    """Record what somebody decided about a conflict (§8.4, T8.7).

    Resolving does **not** re-apply the payload. Whatever went wrong needs a
    person's judgement — the material may have been received on a different
    document, or issued to somebody else, or never have arrived — and a button
    that silently retried would post whichever version happened to win.
    Correcting it is a new submission, and this records that decision.
    """
    from core.audit import record
    from core.models import AuditAction

    if not exception.is_open:
        raise ExceptionNotOpen(
            f"This was already {exception.get_status_display().lower()}: "
            f"{exception.resolution}"
        )
    if not resolution:
        raise ExceptionNotOpen(
            "Say what was done about it. An exception closed with no explanation "
            "is the same as one silently dropped (§8.4)."
        )

    exception.status = (
        ExceptionStatus.DISCARDED if discard else ExceptionStatus.RESOLVED
    )
    exception.resolution = resolution
    exception.resolved_by = resolved_by
    exception.resolved_at = timezone.now()
    exception.save(
        update_fields=["status", "resolution", "resolved_by", "resolved_at", "updated_at"]
    )

    record(
        AuditAction.VARIANCE_RESOLVED,
        actor=resolved_by,
        organization=exception.organization_id,
        target=exception,
        target_label=str(exception),
        request=request,
        after={"status": exception.status},
        note=f"Sync conflict {exception.code}: {resolution}",
    )
    return exception


def open_exceptions(organization_id):
    """What the exceptions register lists from this app (M1, T8.7)."""
    return (
        SyncException.objects.filter(
            organization_id=organization_id, status=ExceptionStatus.OPEN
        )
        .select_related("submission", "submission__submitted_by")
        .order_by("created_at")
    )
