"""Asset services (R14, design §4.20.4).

The register is thin: create, edit, hand over, close, and a filtered read of
fuel. Every write of who holds an asset goes through ``hand_over`` or
``create_asset``/``close_asset`` so the handover history, which is
append-only, is the one truth ``Asset.holder`` projects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.models import User
from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from assets.models import (
    Asset,
    AssetCloseReason,
    AssetHandover,
    AssetStatus,
)
from core.audit import record
from core.exceptions import DomainError, PermissionDeniedError
from core.models import AuditAction

ZERO = Decimal("0.00")

#: What an asset's edit may change. ``holder`` and the closing fields have their
#: own actions, so history is never bypassed.
EDITABLE_FIELDS = (
    "type",
    "name",
    "tag",
    "purchase_date",
    "supplier",
    "cost",
    "purchase_terms",
    "make",
    "model",
    "insurance_expires_on",
    "inspection_expires_on",
)


class AssetClosed(DomainError):
    code = "ASSET_CLOSED"
    status_code = 409
    default_message = "That asset is closed, so it cannot be changed, handed over or take fuel."


class AssetAlreadyWith(DomainError):
    code = "ASSET_ALREADY_WITH"
    status_code = 409
    default_message = "The asset is already with that holder."


class AssetTagDuplicate(DomainError):
    code = "ASSET_TAG_DUPLICATE"
    status_code = 409
    default_message = "Another asset already has that tag or registration."


class AssetDateInFuture(DomainError):
    code = "ASSET_DATE_IN_FUTURE"
    status_code = 400
    default_message = "That date is in the future."


class AssetInvalid(DomainError):
    code = "ASSET_INVALID"
    status_code = 400


def _invalid(field: str, message: str) -> AssetInvalid:
    return AssetInvalid(message, field_errors={field: [message]})


def _today() -> date:
    return timezone.localdate()


def _audit(action, asset, *, actor, note, request=None, before=None, after=None) -> None:  # type: ignore[no-untyped-def]
    record(
        action,
        actor=actor,
        organization=asset.organization_id,
        target=asset,
        target_label=str(asset),
        request=request,
        note=note,
        before=before,
        after=after,
    )


def _clean(asset: Asset) -> None:
    try:
        asset.clean()
    except ValidationError as exc:
        if hasattr(exc, "error_dict"):
            field_errors = {k: [str(m) for m in v] for k, v in exc.message_dict.items()}
        else:
            field_errors = {"non_field_errors": list(exc.messages)}
        first = next(iter(field_errors.values()))[0]
        raise AssetInvalid(first, field_errors=field_errors) from exc


def _check_tag_free(asset: Asset) -> None:
    key = Asset.normalise_tag(asset.tag)
    if not key:
        return
    clash = Asset.objects.filter(tag_key=key).exclude(pk=asset.pk).first()
    if clash is not None:
        raise AssetTagDuplicate(
            f"{clash.name} already has the tag {clash.tag}.",
            details={"asset": {"id": clash.pk, "name": clash.name, "tag": clash.tag}},
            field_errors={"tag": [f"{clash.name} already has that tag."]},
        )


def _check_holder(user: User | None) -> None:
    if user is not None and not user.is_active:
        raise _invalid("to_holder", "That person is not active, so cannot hold an asset.")


def _check_date(on: date | None) -> date:
    on = on or _today()
    if on > _today():
        raise AssetDateInFuture(field_errors={"handed_over_on": ["That date is in the future."]})
    return on


@transaction.atomic
def create_asset(*, actor, holder: User | None = None, request=None, **fields: Any) -> Asset:  # type: ignore[no-untyped-def]
    """Add an asset and write its first handover (§4.20.4)."""
    _check_holder(holder)
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise _invalid(sorted(unknown)[0], "That field cannot be set here.")
    asset = Asset(holder=holder, **fields)
    _clean(asset)
    _check_tag_free(asset)
    try:
        with transaction.atomic():
            asset.save()
    except IntegrityError as exc:  # a racing duplicate tag
        raise AssetTagDuplicate() from exc
    if holder is not None:
        AssetHandover.objects.create(
            organization_id=asset.organization_id,
            asset=asset,
            from_holder=None,
            to_holder=holder,
            handed_over_by=actor,
            handed_over_on=_today(),
            note="Registered",
        )
    _audit(AuditAction.DOCUMENT_POSTED, asset, actor=actor, request=request, note="Asset added.")
    return asset


@transaction.atomic
def update_asset(asset: Asset, *, actor, request=None, **fields: Any) -> Asset:  # type: ignore[no-untyped-def]
    """Edit details. A closed asset is frozen (§4.20.4)."""
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if asset.status == AssetStatus.CLOSED:
        raise AssetClosed()
    before = {f: str(getattr(asset, f)) for f in fields}
    for name, value in fields.items():
        if name not in EDITABLE_FIELDS:
            raise _invalid(name, "That field cannot be changed here.")
        setattr(asset, name, value)
    _clean(asset)
    _check_tag_free(asset)
    try:
        with transaction.atomic():
            asset.save()
    except IntegrityError as exc:
        raise AssetTagDuplicate() from exc
    after = {f: str(getattr(asset, f)) for f in fields}
    _audit(
        AuditAction.DOCUMENT_AMENDED,
        asset,
        actor=actor,
        request=request,
        note="Asset details changed.",
        before=before,
        after=after,
    )
    return asset


@transaction.atomic
def hand_over(  # type: ignore[no-untyped-def]
    asset: Asset,
    *,
    actor,
    to_holder: User | None,
    note: str = "",
    handed_over_on: date | None = None,
    request=None,
) -> AssetHandover:
    """Give the asset to a person, or back to the yard when ``to_holder`` is None.

    Allowed to ``asset.manage`` and to the *current* holder: giving it on is
    natural, taking it from someone is not. The asset row is locked first so two
    simultaneous handovers cannot both start from the same holder (§4.20.4).
    """
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if asset.status == AssetStatus.CLOSED:
        raise AssetClosed()
    if not (
        resolve_permissions(actor).has(PERM.ASSET_MANAGE)
        or (asset.holder_id is not None and asset.holder_id == actor.pk)
    ):
        raise PermissionDeniedError(
            "Only the person holding it, or whoever keeps the asset register, can hand it over."
        )
    to_id = to_holder.pk if to_holder is not None else None
    if to_id == asset.holder_id:
        raise AssetAlreadyWith(
            "It is already in the yard." if to_id is None else f"It is already with {to_holder}.",
            field_errors={"to_holder": ["It is already with them."]},
        )
    _check_holder(to_holder)
    on = _check_date(handed_over_on)
    handover = AssetHandover.objects.create(
        organization_id=asset.organization_id,
        asset=asset,
        from_holder_id=asset.holder_id,
        to_holder=to_holder,
        handed_over_by=actor,
        handed_over_on=on,
        note=(note or "").strip(),
    )
    asset.holder = to_holder
    asset.save(update_fields=["holder", "updated_at"])
    _audit(
        AuditAction.CUSTODY_TRANSFERRED,
        asset,
        actor=actor,
        request=request,
        note=f"Handed over to {to_holder or 'the yard'}.",
    )
    return handover


@transaction.atomic
def close_asset(  # type: ignore[no-untyped-def]
    asset: Asset,
    *,
    actor,
    closed_on: date,
    closed_reason: str,
    closed_note: str = "",
    request=None,
) -> Asset:
    """Sold or written off. Ends the history with a final handover to the yard."""
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if asset.status == AssetStatus.CLOSED:
        raise AssetClosed()
    if closed_reason not in AssetCloseReason.values:
        raise _invalid("closed_reason", "Say whether it was sold or written off.")
    if closed_on is None:
        raise _invalid("closed_on", "Say when.")
    if closed_on > _today():
        raise AssetDateInFuture(field_errors={"closed_on": ["That date is in the future."]})
    if asset.purchase_date and closed_on < asset.purchase_date:
        raise _invalid("closed_on", "That is before it was bought.")
    if asset.holder_id is not None:
        AssetHandover.objects.create(
            organization_id=asset.organization_id,
            asset=asset,
            from_holder_id=asset.holder_id,
            to_holder=None,
            handed_over_by=actor,
            handed_over_on=closed_on,
            note="Closed: " + AssetCloseReason(closed_reason).label.lower(),
        )
        asset.holder = None
    asset.status = AssetStatus.CLOSED
    asset.closed_on = closed_on
    asset.closed_reason = closed_reason
    asset.closed_note = (closed_note or "").strip()
    asset.closed_by = actor
    asset.save()
    _audit(
        AuditAction.STATUS_CHANGED,
        asset,
        actor=actor,
        request=request,
        note=f"Asset closed: {AssetCloseReason(closed_reason).label.lower()} on {closed_on}.",
    )
    return asset


def handovers_of(asset: Asset):  # type: ignore[no-untyped-def]
    """The history, newest first (R14's "every holder")."""
    return asset.handovers.select_related("from_holder", "to_holder", "handed_over_by")


# --------------------------------------------------------------------------
# Fuel by vehicle (a filtered read of the one cost source, §4.20.4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FuelPosition:
    litres: Decimal | None
    spend: Decimal
    fill_count: int
    spend_per_litre: Decimal | None
    pending_spend: Decimal


def _fuel_rows(from_date: date | None, to_date: date | None):  # type: ignore[no-untyped-def]
    from commercials.models import ProjectExpense

    rows = ProjectExpense.objects.filter(vehicle__isnull=False)
    if from_date:
        rows = rows.filter(incurred_on__gte=from_date)
    if to_date:
        rows = rows.filter(incurred_on__lte=to_date)
    return rows


def _signed(qs, field: str) -> Decimal:  # type: ignore[no-untyped-def]
    plus = qs.filter(reverses__isnull=True).aggregate(t=Sum(field))["t"] or ZERO
    minus = qs.filter(reverses__isnull=False).aggregate(t=Sum(field))["t"] or ZERO
    return Decimal(plus) - Decimal(minus)


def _position(rows) -> FuelPosition:  # type: ignore[no-untyped-def]
    """Spend over the same set as ``costing.expense_cost``; pending apart.

    A reversal counts negative, as in the project cost. Per-litre divides the
    spend of the fills that *state* litres by those litres, so a fill with no
    litres recorded cannot make the price look cheap.
    """
    from commercials.models import COSTED_STATUSES, ExpenseStatus

    pending_statuses = (ExpenseStatus.PENDING_PM, ExpenseStatus.PENDING_FINANCE)
    costed = rows.filter(status__in=COSTED_STATUSES)

    spend = _signed(costed, "amount")
    with_litres = costed.filter(litres__isnull=False)
    litres_known = _signed(with_litres, "litres") if with_litres.exists() else None
    per_litre = None
    if litres_known:
        per_litre = (_signed(with_litres, "amount") / litres_known).quantize(Decimal("0.01"))
    cent = Decimal("0.01")
    return FuelPosition(
        litres=litres_known,
        spend=spend.quantize(cent),
        fill_count=costed.filter(reverses__isnull=True).count()
        - costed.filter(reverses__isnull=False).count(),
        spend_per_litre=per_litre,
        pending_spend=_signed(rows.filter(status__in=pending_statuses), "amount").quantize(cent),
    )


def fuel_position(
    asset: Asset, from_date: date | None = None, to_date: date | None = None
) -> FuelPosition:
    """Litres, spend, fills and spend per litre for one asset (§4.20.4)."""
    return _position(_fuel_rows(from_date, to_date).filter(vehicle=asset))


def fuel_ranking(
    from_date: date | None = None, to_date: date | None = None
) -> list[tuple[Asset, FuelPosition]]:
    """Every asset that took fuel in the window, dearest first."""
    rows = _fuel_rows(from_date, to_date)
    ids = rows.values_list("vehicle_id", flat=True).distinct()
    ranked = [
        (asset, _position(rows.filter(vehicle=asset)))
        for asset in Asset.objects.filter(pk__in=ids)
    ]
    ranked.sort(key=lambda pair: pair[1].spend, reverse=True)
    return ranked


def default_window() -> tuple[date, date]:
    today = _today()
    return today - timedelta(days=90), today
