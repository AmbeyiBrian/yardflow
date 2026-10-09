"""Money out: recording, routing, deciding and paying (§4.17; R1-R5).

Every write for an expense or an allowance request goes through here, online or
replayed from a phone (R6), so the rules and the approver check run once, in one
place. The entry's ``status`` is a projection of its ``ApprovalRequest`` rows
(§4.17.1): this module moves the status only as the engine moves the requests.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.models import User, UserRole
from accounts.permissions_registry import PERM
from approvals import engine
from approvals.models import ApprovalDecision
from commercials import finance_rules
from commercials.models import (
    AllowanceRequest,
    AllowanceType,
    Casual,
    ExpenseCasualLine,
    ExpenseCategory,
    ExpenseKind,
    ExpenseStatus,
    ProjectExpense,
    PurchaseDestination,
    SitePurchase,
    SitePurchaseLine,
    TransportScope,
)
from core.audit import client_ip, record
from core.exceptions import DomainError, PermissionDeniedError
from core.models import AuditAction
from core.numbering import DocumentType, allocate_number
from notifications.events import emit
from notifications.matrix import Event

PENDING_STATUSES = (ExpenseStatus.PENDING_PM, ExpenseStatus.PENDING_FINANCE)


# --------------------------------------------------------------------------
# Errors (§4.17.12). The code is the contract; the message is for a person.
# --------------------------------------------------------------------------


class ProjectAmbiguous(DomainError):
    code = "PROJECT_AMBIGUOUS"
    status_code = 400
    default_message = "This site is on more than one open project. Choose the project."


class SiteHasNoOpenProject(DomainError):
    code = "SITE_HAS_NO_OPEN_PROJECT"
    status_code = 400
    default_message = "This site has no open project to cost this to."


class SiteNotOnProject(DomainError):
    code = "SITE_NOT_ON_PROJECT"
    status_code = 400
    default_message = "That project does not include this site."


class ProjectNotOpen(DomainError):
    code = "PROJECT_NOT_OPEN"
    status_code = 400
    default_message = "This project is closed. Its figures are final (O13)."


class SiteOrProjectRequired(DomainError):
    code = "SITE_OR_PROJECT_REQUIRED"
    status_code = 400
    default_message = "Say which site or project this is for."


class AllowanceOverlap(DomainError):
    code = "ALLOWANCE_OVERLAP"
    status_code = 409


class AllowanceLimit(DomainError):
    code = "ALLOWANCE_LIMIT"
    status_code = 400


class TransportScopeRequired(DomainError):
    code = "TRANSPORT_SCOPE_REQUIRED"
    status_code = 400
    default_message = "Say whether this transport is within Nairobi or outside it."


class CasualIdDuplicate(DomainError):
    code = "CASUAL_ID_DUPLICATE"
    status_code = 409


class FinanceNoOtherApprover(DomainError):
    code = "FINANCE_NO_OTHER_APPROVER"
    status_code = 409
    default_message = (
        "Nobody except you can give the Finance approval, so this could never be "
        "approved. Ask an owner to give someone else the finance.approve permission."
    )


class FinanceSelfApproval(DomainError):
    code = "FINANCE_SELF_APPROVAL"
    status_code = 403
    default_message = "You recorded this, so you cannot decide it. Ask another approver."


class FinanceNotDecidable(DomainError):
    code = "FINANCE_NOT_DECIDABLE"
    status_code = 409
    default_message = "This is not in a state where that can be done."


class FloatNotOpen(DomainError):
    code = "FLOAT_NOT_OPEN"
    status_code = 409
    default_message = "That float is not open for you to spend from."


class PaymentReferenceRequired(DomainError):
    code = "PAYMENT_REFERENCE_REQUIRED"
    status_code = 400
    default_message = "Enter the payment reference, for example the M-Pesa code."


class FloatBackedNotPayable(DomainError):
    """It was paid out of a float already (§4.17.2), so paying it would be twice."""

    code = "FLOAT_BACKED_NOT_PAYABLE"
    status_code = 409
    default_message = "This was spent from a float that is already paid, so it is not paid again."


class OverBudgetReasonRequired(DomainError):
    """R9: past the budget is allowed, but never silently (§4.19.5)."""

    code = "OVER_BUDGET_REASON_REQUIRED"
    status_code = 400
    default_message = "This is over the project's budget. Say why."


class SitePurchaseYardNeedsCatalogue(DomainError):
    """Goods going into the yard have to be catalogue items to be received (§4.19.3)."""

    code = "SITE_PURCHASE_YARD_NEEDS_CATALOGUE"
    status_code = 400
    default_message = (
        "Goods going into the yard must be catalogue items, so they can be received into stock."
    )


class SupplierNotUsableOnPurchase(DomainError):
    """A deactivated or rejected supplier cannot be bought from (R15, §4.20.3)."""

    code = "SUPPLIER_NOT_USABLE"
    status_code = 400
    default_message = "That supplier is not active, or was rejected, so it cannot be used."


class RejectionReasonRequired(DomainError):
    code = "REJECTION_REASON_REQUIRED"
    status_code = 400
    default_message = "Rejecting needs a reason, so the person knows what to fix."


class FinanceInputInvalid(DomainError):
    """A field the entry cannot do without, named so a form can show it."""

    code = "FINANCE_INPUT_INVALID"
    status_code = 400


def _invalid(field: str, message: str) -> FinanceInputInvalid:
    return FinanceInputInvalid(message, field_errors={field: [message]})


# --------------------------------------------------------------------------
# Site to project (R1, §4.17.4)
# --------------------------------------------------------------------------


def open_projects_of(site):  # type: ignore[no-untyped-def]
    """The open projects at ``site``, by reference. Never raises (§4.18.5 step 5).

    Empty when there are none; clock-in and ``resolve_project`` each decide what
    that, or several, means.
    """
    from network.models import ProjectStatus

    return list(site.projects.filter(status=ProjectStatus.OPEN).order_by("reference"))


def resolve_project(site, project=None):  # type: ignore[no-untyped-def]
    """The project an entry at ``site`` is costed to (§4.17.4).

    One open project is chosen automatically; two or more need the person to
    say which, since guessing would put money on the wrong budget. A project
    given without a site is accepted as it stands (a permit for the PO).
    """
    from network.models import ProjectStatus

    if site is None:
        if project is None:
            raise SiteOrProjectRequired()
        if project.status != ProjectStatus.OPEN:
            raise ProjectNotOpen()
        return project

    on_site = site.projects.all()
    if project is not None:
        if not on_site.filter(pk=project.pk).exists():
            raise SiteNotOnProject(
                f"{project} is not one of the projects at {site}.",
                details={"site": str(site), "project": str(project)},
            )
        if project.status != ProjectStatus.OPEN:
            raise ProjectNotOpen()
        return project

    open_projects = open_projects_of(site)
    if not open_projects:
        raise SiteHasNoOpenProject(
            f"{site} has no open project to cost this to.",
            details={"site": str(site)},
        )
    if len(open_projects) > 1:
        raise ProjectAmbiguous(
            f"{site} is on {len(open_projects)} open projects. Choose which one.",
            details={
                "candidates": [
                    {"id": item.pk, "reference": item.reference, "label": str(item)}
                    for item in open_projects
                ]
            },
        )
    return open_projects[0]


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _audit(action, entry, *, actor, note: str, request=None) -> None:  # type: ignore[no-untyped-def]
    record(
        action,
        actor=actor,
        organization=entry.organization_id,
        target=entry,
        target_label=str(entry),
        request=request,
        note=note,
    )


def has_other_finance_approver(recorder) -> bool:  # type: ignore[no-untyped-def]
    """Whether anyone but ``recorder`` could give the Finance approval (R4).

    Held directly: a delegation does not lend a Finance signature (D22), so it
    does not count here any more than it does in ``can_approve``.
    """
    return (
        UserRole.objects.filter(
            user__is_active=True, role__permissions__codename=PERM.FINANCE_APPROVE
        )
        .exclude(user_id=recorder.pk)
        .exists()
    )


def _require_other_approver(recorder) -> None:  # type: ignore[no-untyped-def]
    if not has_other_finance_approver(recorder):
        raise FinanceNoOtherApprover()


def _route(entry: ProjectExpense | AllowanceRequest | SitePurchase, actor) -> None:  # type: ignore[no-untyped-def]
    """Create the approval requests and set the status they imply.

    A skipped PM level leaves only the level-2 request, so the entry starts at
    ``PENDING_FINANCE``. The engine raises ``ProjectHasNoActiveManager`` when the
    PM level is needed and there is nobody to answer it (D28); the caller's
    transaction then rolls the entry back with it.
    """
    requests = engine.create_requests(entry, requested_by=actor)
    first_level = min(item.level for item in requests)
    target = (
        ExpenseStatus.PENDING_PM
        if first_level == engine.FINANCE_PM_LEVEL
        else ExpenseStatus.PENDING_FINANCE
    )
    if entry.status != target:
        entry.status = target
        entry.save()
    # Recording and sending again both come through here, so both tell whoever
    # holds the first open level (R4, §4.17.9).
    _notify(entry, Event.FINANCE_AWAITING_APPROVAL)


def _notify(entry, event_key: str) -> None:  # type: ignore[no-untyped-def]
    """Tell people about an entry (R4, §4.17.9).

    Called inside the caller's transaction: ``emit`` sends only after commit, so
    a failed send never undoes an approval and a rolled-back one sends nothing
    (L3).
    """
    emit(event_key, entry)


def over_budget_check(  # type: ignore[no-untyped-def]
    project,
    amount: Decimal,
    *,
    reason: str,
    offline: bool,
    request=None,
) -> tuple[Decimal | None, str]:
    """R9 (§4.19.5): what to record on an entry that may break the budget.

    Returns ``(over_budget_by, reason)``. Online, going over without a reason
    raises ``OVER_BUDGET_REASON_REQUIRED``. On **replay** it never refuses: the
    phone's figures may be stale, so the overrun is recorded with whatever
    reason there is (possibly none) for the approver to see. The overrun is
    named in the error only for those who may see project cost.
    """
    from commercials import budget
    from commercials.visibility import may_see_project_cost

    reason = (reason or "").strip()
    over_by = budget.would_exceed(project, amount)
    if over_by is None:
        return None, ""
    if not reason and not offline:
        details: dict[str, Any] = {"over": True}
        if may_see_project_cost(request, project):
            details["over_by"] = str(over_by)
        raise OverBudgetReasonRequired(
            details=details, field_errors={"over_budget_reason": ["Say why."]}
        )
    return over_by, reason


def _existing(model, client_uuid):  # type: ignore[no-untyped-def]
    if client_uuid is None:
        return None
    return model.objects.filter(client_uuid=client_uuid).first()


def _lock_status(entry) -> str:  # type: ignore[no-untyped-def]
    """Lock the row and return the status it has *now*.

    Compared with the caller's copy rather than refreshed into it: a refresh
    would leave the save guard comparing against the status the object was
    loaded with, not the one it now has.
    """
    current = (
        type(entry)
        .objects.select_for_update()
        .filter(pk=entry.pk)
        .values_list("status", flat=True)
        .first()
    )
    if current is None:
        raise FinanceNotDecidable("This no longer exists.")
    return str(current)


def _require_status(entry, allowed: Iterable[str], what: str) -> None:  # type: ignore[no-untyped-def]
    current = _lock_status(entry)
    if current != entry.status or current not in set(allowed):
        label = ExpenseStatus(current).label.lower()
        raise FinanceNotDecidable(f"This cannot be {what}: it is {label}.")


def _check_float(float_request, recorder) -> None:  # type: ignore[no-untyped-def]
    """The float an expense is spent from: the recorder's own, paid, and open (R2)."""
    if (
        float_request.recorded_by_id != recorder.pk
        or float_request.type != AllowanceType.FLOAT
        or float_request.status != ExpenseStatus.PAID
        or float_request.closed_at is not None
    ):
        raise FloatNotOpen(
            f"{float_request.number or 'That float'} is not a paid, open float of yours."
        )


# --------------------------------------------------------------------------
# Recording an expense (R1)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CasualLineInput:
    """One casual's days on a casual-labour expense (R1, R3)."""

    casual: Casual
    days: int
    amount: Decimal | None = None


class FuelVehicleRequired(DomainError):
    code = "FUEL_VEHICLE_REQUIRED"
    status_code = 400
    default_message = "Fuel needs the vehicle: pick one from the register or type its registration."


class FuelVehicleMismatch(DomainError):
    code = "FUEL_VEHICLE_MISMATCH"
    status_code = 400
    default_message = "The registration typed is not the registration of the vehicle picked."


def _fuel_vehicle(category, vehicle, vehicle_reg: str) -> str:  # type: ignore[no-untyped-def]
    """Settle a fuel expense's vehicle and return the ``vehicle_reg`` to store (R14, §4.20.4).

    A FUEL expense names exactly one of an open VEHICLE or GENERATOR from the
    register, or a typed registration "not on the register". A typed
    registration that restates the picked vehicle's tag is tolerated (old
    clients send both); a different one is a mismatch. Old payloads sending
    only ``vehicle_reg`` still work, so queued expenses replay.
    """
    from assets.models import AssetStatus, AssetType
    from assets.services import AssetClosed

    typed = (vehicle_reg or "").strip()
    if vehicle is not None:
        vehicle.refresh_from_db()  # a closed-since view must not take fuel
    if category.kind != ExpenseKind.FUEL:
        if vehicle is not None:
            raise _invalid("vehicle", "Only a fuel expense names a vehicle.")
        return typed
    if vehicle is None:
        if not typed:
            message = FuelVehicleRequired.default_message
            raise FuelVehicleRequired(field_errors={"vehicle_reg": [message]})
        return typed
    if vehicle.type not in (AssetType.VEHICLE, AssetType.GENERATOR):
        raise _invalid("vehicle", "Fuel goes into a vehicle or a generator.")
    if vehicle.status == AssetStatus.CLOSED:
        raise AssetClosed(field_errors={"vehicle": ["That asset is closed."]})
    if typed and vehicle.normalise_tag(typed) != vehicle.tag_key:
        message = FuelVehicleMismatch.default_message
        raise FuelVehicleMismatch(field_errors={"vehicle_reg": [message]})
    max_length = ProjectExpense._meta.get_field("vehicle_reg").max_length or 20
    return (vehicle.tag or "")[:max_length]


def record_expense(
    *,
    actor,  # type: ignore[no-untyped-def]
    category: ExpenseCategory,
    amount: Decimal,
    incurred_on: date,
    site=None,  # type: ignore[no-untyped-def]
    project=None,  # type: ignore[no-untyped-def]
    job=None,  # type: ignore[no-untyped-def]
    description: str = "",
    scope_of_work: str = "",
    vehicle_reg: str = "",
    vehicle=None,  # type: ignore[no-untyped-def]
    litres: Decimal | None = None,
    float_request: AllowanceRequest | None = None,
    photos_expected: int = 0,
    casual_lines: Iterable[CasualLineInput] = (),
    client_uuid: UUID | None = None,
    over_budget_reason: str = "",
    offline: bool = False,
    request=None,  # type: ignore[no-untyped-def]
) -> ProjectExpense:
    """Record an expense and send it for approval (R1, R4, §4.17.3).

    Idempotent on ``client_uuid``: a phone that lost the response sends it
    again, and gets the row it already made (R6).
    """
    existing = _existing(ProjectExpense, client_uuid)
    if existing is not None:
        return existing

    lines = list(casual_lines)
    resolved = resolve_project(site, project)
    if job is not None and job.project_id != resolved.pk:
        raise _invalid("job", "That job belongs to a different project.")
    if amount is None or amount <= 0:
        raise _invalid("amount", "The amount must be more than zero.")

    vehicle_reg = _fuel_vehicle(category, vehicle, vehicle_reg)
    if category.kind == ExpenseKind.CASUAL_LABOUR:
        if not lines:
            raise _invalid(
                "casual_lines", "Casual labour needs at least one casual and their days."
            )
    elif lines:
        raise _invalid("casual_lines", "Only a casual-labour expense names casuals.")
    seen: set[int] = set()
    for line in lines:
        if line.days is None or line.days <= 0:
            raise _invalid("casual_lines", "Each casual needs at least one day.")
        if line.casual.pk in seen:
            raise _invalid("casual_lines", f"{line.casual.name} is listed twice.")
        seen.add(line.casual.pk)

    if float_request is not None:
        _check_float(float_request, actor)
    _require_other_approver(actor)
    # R9: a float-backed expense spends money already budgeted when the float
    # was requested, so it is not checked again (§4.19.5).
    over_by, over_reason = (
        (None, "")
        if float_request is not None
        else over_budget_check(
            resolved, amount, reason=over_budget_reason, offline=offline, request=request
        )
    )

    try:
        with transaction.atomic():
            expense = ProjectExpense.objects.create(
                project=resolved,
                site=site,
                job=job,
                category=category,
                amount=amount,
                incurred_on=incurred_on,
                description=description,
                scope_of_work=scope_of_work,
                vehicle_reg=vehicle_reg,
                vehicle=vehicle,
                litres=litres,
                float_request=float_request,
                photos_expected=photos_expected,
                client_uuid=client_uuid,
                recorded_by=actor,
                over_budget_by=over_by,
                over_budget_reason=over_reason,
            )
            for line in lines:
                ExpenseCasualLine.objects.create(
                    expense=expense, casual=line.casual, days=line.days, amount=line.amount
                )
            _route(expense, actor)
            _audit(
                AuditAction.DOCUMENT_POSTED,
                expense,
                actor=actor,
                request=request,
                note=f"Expense recorded on {resolved}; now {expense.get_status_display().lower()}.",
            )
    except IntegrityError:
        # Two sends of one client_uuid raced past the check above; the unique
        # constraint kept one, and the other is simply a replay.
        replay = _existing(ProjectExpense, client_uuid)
        if replay is None:
            raise
        return replay
    return expense


# --------------------------------------------------------------------------
# Recording a site purchase (R7, §4.19.3)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PurchaseLineInput:
    """One thing bought: a catalogue item or free text, a quantity and a price."""

    quantity: Decimal
    unit_price: Decimal
    item_type: Any = None  # catalogue.ItemType | None
    description: str = ""


def _round_line(quantity: Decimal, unit_price: Decimal) -> Decimal:
    return (quantity * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _check_supplier_recordable(supplier) -> None:  # type: ignore[no-untyped-def]
    """Active and not REJECTED; PENDING may be bought from, never paid (R15)."""
    from network.models import SupplierStatus

    if supplier is None:
        raise _invalid("supplier", "Choose the supplier.")
    if not supplier.is_active or supplier.status == SupplierStatus.REJECTED:
        raise SupplierNotUsableOnPurchase(
            f"{supplier.name} cannot be used: it is inactive or was rejected.",
            field_errors={"supplier": ["Not usable."]},
        )


def record_site_purchase(
    *,
    actor,  # type: ignore[no-untyped-def]
    site,  # type: ignore[no-untyped-def]
    supplier,  # type: ignore[no-untyped-def]
    purchase_date: date,
    lines: Iterable[PurchaseLineInput],
    destination: str = PurchaseDestination.USED_AT_SITE,
    receive_into=None,  # type: ignore[no-untyped-def]
    project=None,  # type: ignore[no-untyped-def]
    photos_expected: int = 0,
    client_uuid: UUID | None = None,
    over_budget_reason: str = "",
    offline: bool = False,
    request=None,  # type: ignore[no-untyped-def]
) -> SitePurchase:
    """Record goods bought on the spot and send them for approval (R7, §4.19.3).

    Same shape as ``record_expense``: idempotent on ``client_uuid`` (R6), the
    project resolved from the site, a second approver required, the budget
    checked (never refusing a replay), then routed PM then Finance. An
    INTO_YARD purchase also needs somewhere to receive it and catalogue items
    on every line; its draft delivery is made at final approval.
    """
    existing = _existing(SitePurchase, client_uuid)
    if existing is not None:
        return existing

    if site is None:
        raise _invalid("site", "Say which site this was bought for.")
    resolved = resolve_project(site, project)
    _check_supplier_recordable(supplier)
    if destination not in PurchaseDestination.values:
        raise _invalid("destination", "Choose where the goods are going.")
    if purchase_date is None:
        raise _invalid("purchase_date", "Enter the date of the purchase.")

    items = list(lines)
    if not items:
        raise _invalid("lines", "Add at least one line.")
    for item in items:
        if item.quantity is None or item.quantity <= 0:
            raise _invalid("lines", "Each line needs a quantity above zero.")
        if item.unit_price is None or item.unit_price < 0:
            raise _invalid("lines", "A unit price cannot be below zero.")
        if item.unit_price != item.unit_price.quantize(Decimal("0.01")):
            raise _invalid("lines", "A unit price has at most two decimal places.")
        if item.quantity != item.quantity.quantize(Decimal("0.001")):
            raise _invalid("lines", "A quantity has at most three decimal places.")
        if item.item_type is None and not (item.description or "").strip():
            raise _invalid("lines", "Each line needs an item or a description.")

    if destination == PurchaseDestination.INTO_YARD:
        from locations.models import LocationType

        if receive_into is None:
            raise _invalid("receive_into", "Which yard or store will it be received into?")
        if receive_into.type not in (LocationType.YARD, LocationType.STORE):
            raise _invalid("receive_into", "Choose a yard or a store.")
        if any(item.item_type is None for item in items):
            raise SitePurchaseYardNeedsCatalogue(
                field_errors={"lines": [str(SitePurchaseYardNeedsCatalogue.default_message)]}
            )
    else:
        receive_into = None

    amount = sum((_round_line(i.quantity, i.unit_price) for i in items), Decimal("0.00"))
    if amount <= 0:
        raise _invalid("lines", "The purchase must come to more than zero.")

    _require_other_approver(actor)
    over_by, over_reason = over_budget_check(
        resolved, amount, reason=over_budget_reason, offline=offline, request=request
    )

    try:
        with transaction.atomic():
            purchase = SitePurchase.objects.create(
                number=allocate_number(DocumentType.SITE_PURCHASE),
                project=resolved,
                site=site,
                supplier=supplier,
                purchase_date=purchase_date,
                destination=destination,
                receive_into=receive_into,
                amount=amount,
                recorded_by=actor,
                photos_expected=photos_expected,
                client_uuid=client_uuid,
                over_budget_by=over_by,
                over_budget_reason=over_reason,
            )
            for item in items:
                SitePurchaseLine.objects.create(
                    purchase=purchase,
                    item_type=item.item_type,
                    description=(item.description or "").strip(),
                    quantity=item.quantity,
                    uom=item.item_type.uom if item.item_type is not None else "",
                    unit_price=item.unit_price,
                )
            _route(purchase, actor)
            _audit(
                AuditAction.DOCUMENT_POSTED,
                purchase,
                actor=actor,
                request=request,
                note=f"{purchase.number} recorded on {resolved}; "
                f"now {purchase.get_status_display().lower()}.",
            )
    except IntegrityError:
        replay = _existing(SitePurchase, client_uuid)
        if replay is None:
            raise
        return replay
    return purchase


# --------------------------------------------------------------------------
# Requesting an allowance (R2, R5)
# --------------------------------------------------------------------------

_TYPE_LABELS = dict(AllowanceType.choices)


def _check_rules(
    *,
    recorder,  # type: ignore[no-untyped-def]
    allowance_type: str,
    transport_scope: str,
    amount: Decimal,
    from_date: date,
    to_date: date,
    exclude_pk: int | None = None,
) -> None:
    """Overlap, then limits (§4.17.5). Call with the recorder's user row locked."""
    earlier = AllowanceRequest.objects.filter(
        recorded_by=recorder, type=allowance_type
    ).exclude(pk=exclude_pk)
    clash = finance_rules.find_overlap(allowance_type, from_date, to_date, earlier)
    if clash is not None:
        raise AllowanceOverlap(
            f"You already have {clash.number} for {_TYPE_LABELS[allowance_type].lower()} "
            f"from {clash.from_date:%d %b %Y} to {clash.to_date:%d %b %Y}, which "
            f"overlaps these dates.",
            details={"earlier": clash.number},
        )

    days = finance_rules.days_between(from_date, to_date)
    limits = recorder.organization.settings.allowance_limits or {}
    key = finance_rules.limit_key(allowance_type, transport_scope)
    breach = finance_rules.check_limit(key, amount, days, limits)
    if breach is not None:
        word = "below the minimum" if breach.side == "min" else "above the limit"
        raise AllowanceLimit(
            f"That is KES {breach.daily:,.2f} a day, {word} of KES {breach.bound:,.2f} a day "
            f"for {_TYPE_LABELS[allowance_type].lower()}.",
            details={
                "daily": str(breach.daily.quantize(Decimal("0.01"))),
                "limit": str(breach.bound),
                "side": breach.side,
                "key": breach.key,
            },
        )


def _lock_recorder(recorder) -> None:  # type: ignore[no-untyped-def]
    """Serialise one person's requests, so two sends cannot both pass overlap.

    The rows the rule reads may not exist yet, so there is nothing to lock but
    the person (§4.17.5).
    """
    User.objects.select_for_update().filter(pk=recorder.pk).first()


def request_allowance(
    *,
    actor,  # type: ignore[no-untyped-def]
    type: str,
    amount: Decimal,
    from_date: date,
    to_date: date,
    site=None,  # type: ignore[no-untyped-def]
    project=None,  # type: ignore[no-untyped-def]
    reason: str = "",
    transport_scope: str = "",
    client_uuid: UUID | None = None,
    over_budget_reason: str = "",
    offline: bool = False,
    request=None,  # type: ignore[no-untyped-def]
) -> AllowanceRequest:
    """Ask for money before it is spent, subject to the R5 rules (§4.17.5).

    Idempotent on ``client_uuid`` (R6). The rules run here, so a request
    replayed from a phone is judged exactly as one made online.
    """
    existing = _existing(AllowanceRequest, client_uuid)
    if existing is not None:
        return existing

    if type not in AllowanceType.values:
        raise _invalid("type", "Choose a type of request.")
    if amount is None or amount <= 0:
        raise _invalid("amount", "The amount must be more than zero.")
    if to_date < from_date:
        raise _invalid("to_date", "The end date cannot be before the start date.")
    if type == AllowanceType.TRANSPORT:
        if transport_scope not in TransportScope.values:
            raise TransportScopeRequired()
    else:
        # Only transport has a scope (a CHECK says so); dropping a stray one is
        # kinder than refusing a request over a field that does not apply.
        transport_scope = ""

    resolved = resolve_project(site, project)
    _require_other_approver(actor)

    try:
        with transaction.atomic():
            _lock_recorder(actor)
            _check_rules(
                recorder=actor,
                allowance_type=type,
                transport_scope=transport_scope,
                amount=amount,
                from_date=from_date,
                to_date=to_date,
            )
            over_by, over_reason = over_budget_check(
                resolved,
                amount,
                reason=over_budget_reason,
                offline=offline,
                request=request,
            )
            allowance = AllowanceRequest.objects.create(
                number=allocate_number(DocumentType.ALLOWANCE),
                type=type,
                transport_scope=transport_scope,
                amount=amount,
                from_date=from_date,
                to_date=to_date,
                site=site,
                project=resolved,
                reason=reason,
                recorded_by=actor,
                client_uuid=client_uuid,
                over_budget_by=over_by,
                over_budget_reason=over_reason,
            )
            _route(allowance, actor)
            _audit(
                AuditAction.DOCUMENT_POSTED,
                allowance,
                actor=actor,
                request=request,
                note=f"{allowance.number} requested on {resolved}; "
                f"now {allowance.get_status_display().lower()}.",
            )
    except IntegrityError:
        replay = _existing(AllowanceRequest, client_uuid)
        if replay is None:
            raise
        return replay
    return allowance


# --------------------------------------------------------------------------
# Deciding, resubmitting, paying
# --------------------------------------------------------------------------


def decide[Entry: (ProjectExpense, AllowanceRequest, SitePurchase)](
    entry: Entry,
    *,
    actor,  # type: ignore[no-untyped-def]
    approved: bool,
    reason: str = "",
    request=None,  # type: ignore[no-untyped-def]
) -> Entry:
    """Approve or reject at the caller's level (R4, §4.17.3).

    The engine decides whether ``actor`` may answer the open level; this moves
    the status to match. A PM approval passes the entry to Finance, Finance's
    makes it ``APPROVED`` (and so costed), and a rejection at either level
    returns it to its recorder with every later level superseded.
    """
    if entry.status not in PENDING_STATUSES:
        raise FinanceNotDecidable(
            f"This was already {entry.get_status_display().lower()}."
        )
    # Before the engine, so the answer is the finance one (403, its own code)
    # at both levels, even for a PM who is also the recorder.
    if entry.recorded_by_id == actor.pk:
        raise FinanceSelfApproval()
    reason = reason.strip()
    if not approved and not reason:
        raise RejectionReasonRequired()

    with transaction.atomic():
        _require_status(entry, PENDING_STATUSES, "decided")
        decided, upcoming = engine.record_decision(
            entry,
            actor=actor,
            decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED,
            reason=reason,
            ip=client_ip(request) if request is not None else None,
            user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:400]
            if request is not None
            else "",
        )

        if not approved:
            entry.status = ExpenseStatus.REJECTED
            _stamp_decision(entry, actor, reason)
            entry.save()
            note = f"Rejected at level {decided.level}: {reason}"
            action = AuditAction.REJECTED
            _notify(entry, Event.FINANCE_REJECTED)
        elif upcoming is not None:
            entry.status = ExpenseStatus.PENDING_FINANCE
            entry.save()
            note = f"Approved at level {decided.level}; now waiting on Finance."
            action = AuditAction.APPROVED
            _notify(entry, Event.FINANCE_AWAITING_APPROVAL)
        else:
            if entry.status == ExpenseStatus.PENDING_PM:
                # The guard allows only the §4.17.3 moves; an entry with no
                # Finance level left still passes through it.
                entry.status = ExpenseStatus.PENDING_FINANCE
                entry.save()
            if (
                isinstance(entry, SitePurchase)
                and entry.destination == PurchaseDestination.INTO_YARD
                and entry.reverses_id is None
            ):
                # R7: the goods are expected, so the storekeeper gets a draft.
                # Made before the status changes (an approved purchase is
                # frozen) and inside this transaction, so a failure leaves the
                # purchase unapproved (YARD_DELIVERY_FAILED).
                from receiving.services import draft_gate_in_for_purchase

                draft_gate_in_for_purchase(entry, actor)
            entry.status = ExpenseStatus.APPROVED
            _stamp_decision(entry, actor, "")
            entry.save()
            note = "Approved by Finance."
            action = AuditAction.APPROVED
            _notify(entry, Event.FINANCE_APPROVED)
        _audit(action, entry, actor=actor, request=request, note=note)
    return entry


def _stamp_decision(entry, actor, reason: str) -> None:  # type: ignore[no-untyped-def]
    entry.decided_by = actor
    entry.decided_at = timezone.now()
    entry.decision_reason = reason


def resubmit[Entry: (ProjectExpense, AllowanceRequest, SitePurchase)](
    entry: Entry,
    *,
    actor,  # type: ignore[no-untyped-def]
    request=None,  # type: ignore[no-untyped-def]
) -> Entry:
    """Send a rejected entry round again (R4, §4.17.3).

    Recorder only. It goes to its first applicable level with new requests; the
    old ones stay, since they are the record of what was said the first time.
    The R5 rules and the approver check run again: the dates may have been
    taken meanwhile, and the limits or the approvers changed.
    """
    if entry.recorded_by_id != actor.pk:
        raise PermissionDeniedError("Only the person who recorded this can send it again.")
    if entry.status != ExpenseStatus.REJECTED:
        raise FinanceNotDecidable("Only a rejected entry can be sent again.")

    _require_other_approver(actor)
    with transaction.atomic():
        _lock_recorder(actor)
        _require_status(entry, [ExpenseStatus.REJECTED], "sent again")
        if isinstance(entry, AllowanceRequest):
            _check_rules(
                recorder=actor,
                allowance_type=entry.type,
                transport_scope=entry.transport_scope,
                amount=entry.amount,
                from_date=entry.from_date,
                to_date=entry.to_date,
                exclude_pk=entry.pk,
            )
        elif isinstance(entry, SitePurchase):
            _check_supplier_recordable(entry.supplier)
        elif entry.float_request is not None:
            _check_float(entry.float_request, actor)

        # Cleared first: the CHECK allows no decision on a pending entry.
        entry.status = ExpenseStatus.PENDING_PM
        entry.decided_by = None
        entry.decided_at = None
        entry.decision_reason = ""
        entry.save()
        _route(entry, actor)
        _audit(
            AuditAction.STATUS_CHANGED,
            entry,
            actor=actor,
            request=request,
            note=f"Sent again; now {entry.get_status_display().lower()}.",
        )
    return entry


def mark_paid[Entry: (ProjectExpense, AllowanceRequest, SitePurchase)](
    entry: Entry,
    *,
    actor,  # type: ignore[no-untyped-def]
    reference: str,
    paid_at: datetime | None = None,
    request=None,  # type: ignore[no-untyped-def]
) -> Entry:
    """Record that Finance paid an approved entry (R4, D24).

    YardFlow records the payment; it does not send money.
    """
    reference = (reference or "").strip()
    if not reference:
        raise PaymentReferenceRequired()
    if entry.status != ExpenseStatus.APPROVED:
        raise FinanceNotDecidable(
            f"Only an approved entry is paid; this is {entry.get_status_display().lower()}."
        )
    if isinstance(entry, ProjectExpense):
        if entry.float_request_id is not None:
            raise FloatBackedNotPayable()
        if entry.reverses_id is not None:
            raise FinanceNotDecidable("A reversal is not paid.")
    if isinstance(entry, SitePurchase):
        if entry.reverses_id is not None:
            raise FinanceNotDecidable("A reversal is not paid.")
        if entry.reversals.exists():
            raise FinanceNotDecidable("This purchase was reversed, so it is not paid.")
        # R15 (§4.20.3): recording may name a PENDING supplier; paying may not.
        from network.suppliers import SupplierNotApproved, assert_payable

        if entry.supplier is None:
            raise SupplierNotApproved()
        assert_payable(entry.supplier)

    with transaction.atomic():
        _require_status(entry, [ExpenseStatus.APPROVED], "paid")
        entry.status = ExpenseStatus.PAID
        entry.paid_at = paid_at or timezone.now()
        entry.paid_by = actor
        entry.payment_reference = reference
        entry.save()
        _notify(entry, Event.FINANCE_PAID)
        _audit(
            AuditAction.STATUS_CHANGED,
            entry,
            actor=actor,
            request=request,
            note=f"Paid, reference {reference}.",
        )
    return entry


# --------------------------------------------------------------------------
# Floats (R2)
# --------------------------------------------------------------------------


def float_spent(float_request: AllowanceRequest) -> Decimal:
    """Expenses charged to the float that have not been rejected.

    Pending ones count: the money is already out of the person's hands.
    """
    total = (
        float_request.expenses.exclude(status=ExpenseStatus.REJECTED)
        .aggregate(total=Sum("amount"))["total"]
    )
    return total or Decimal("0.00")


def float_balance(float_request: AllowanceRequest) -> Decimal:
    """``amount − spent − returned`` (§4.17.2); negative reads "owed to you"."""
    return finance_rules.float_balance(
        float_request.amount, float_spent(float_request), float_request.returned_amount
    )


def open_float_warning(recorder, exclude: AllowanceRequest | None = None) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    """Another paid, unclosed float of ``recorder``'s, for approvers to see (R2).

    A warning only: a second float is never blocked.
    """
    other = (
        AllowanceRequest.objects.filter(
            recorded_by=recorder,
            type=AllowanceType.FLOAT,
            status=ExpenseStatus.PAID,
            closed_at__isnull=True,
        )
        .exclude(pk=exclude.pk if exclude is not None else None)
        .order_by("number")
        .first()
    )
    if other is None:
        return None
    return {"number": other.number, "balance": float_balance(other)}


def close_float(
    float_request: AllowanceRequest,
    *,
    actor,  # type: ignore[no-untyped-def]
    returned_amount: Decimal,
    request=None,  # type: ignore[no-untyped-def]
) -> AllowanceRequest:
    """Close a paid float, recording what came back (R2)."""
    if returned_amount is None or returned_amount < 0:
        raise _invalid("returned_amount", "What came back cannot be less than zero.")

    with transaction.atomic():
        current = (
            AllowanceRequest.objects.select_for_update()
            .filter(pk=float_request.pk)
            .values_list("status", "closed_at", "type")
            .first()
        )
        if (
            current is None
            or current[2] != AllowanceType.FLOAT
            or current[0] != ExpenseStatus.PAID
            or current[1] is not None
        ):
            raise FloatNotOpen(f"{float_request.number} is not a paid, open float.")
        float_request.closed_at = timezone.now()
        float_request.closed_by = actor
        float_request.returned_amount = returned_amount
        float_request.save()
        _audit(
            AuditAction.STATUS_CHANGED,
            float_request,
            actor=actor,
            request=request,
            note=f"Float closed; KES {returned_amount:,.2f} returned.",
        )
    return float_request


# --------------------------------------------------------------------------
# Casuals (R3)
# --------------------------------------------------------------------------


def register_casual(
    *,
    actor,  # type: ignore[no-untyped-def]
    name: str,
    id_number: str,
    phone: str = "",
    client_uuid: UUID | None = None,
    request=None,  # type: ignore[no-untyped-def]
) -> Casual:
    """Add a casual to the register, once (R3).

    The same ID number twice is refused, naming the record that has it. A
    replay of one ``client_uuid`` returns the row it made (R6).
    """
    existing = _existing(Casual, client_uuid)
    if existing is not None:
        return existing

    name = name.strip()
    key = Casual.normalise_id_number(id_number or "")
    if not name:
        raise _invalid("name", "Enter the casual's name.")
    if not key:
        raise _invalid("id_number", "Enter the casual's ID number.")

    def duplicate() -> CasualIdDuplicate:
        match = Casual.objects.get(id_number_key=key)
        return CasualIdDuplicate(
            f"{match.name} is already registered with that ID number.",
            details={"existing": {"id": match.pk, "name": match.name}},
        )

    if Casual.objects.filter(id_number_key=key).exists():
        raise duplicate()

    try:
        with transaction.atomic():
            casual = Casual.objects.create(
                name=name,
                id_number=id_number.strip(),
                phone=phone.strip(),
                registered_by=actor,
                client_uuid=client_uuid,
            )
            _audit(
                AuditAction.DOCUMENT_POSTED,
                casual,
                actor=actor,
                request=request,
                note=f"Casual {name} registered.",
            )
    except IntegrityError:
        replay = _existing(Casual, client_uuid)
        if replay is not None:
            return replay
        if Casual.objects.filter(id_number_key=key).exists():
            raise duplicate() from None
        raise
    return casual
