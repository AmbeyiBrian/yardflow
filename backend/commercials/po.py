"""The PO that arrives after the work started (R12; design §4.19.8).

A project without a PO is the work-order project that already exists (O1), so
adding the PO is not a conversion: the same row gains its PO number, value,
budget and terms. Every job, expense, gate-out and attachment already on it
stays attached, which is the whole point.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from commercials.milestones import seed_default_milestones
from core.audit import record
from core.exceptions import DomainError
from core.models import AuditAction
from network.models import Project, ProjectStatus
from notifications.events import emit_po_attached


class ProjectAlreadyHasPo(DomainError):
    code = "PROJECT_ALREADY_HAS_PO"
    status_code = 409
    default_message = (
        "This project already has a PO. A changed value is a variation, not a new PO."
    )


class ProjectNotOpen(DomainError):
    code = "PROJECT_NOT_OPEN"
    status_code = 409
    default_message = "This project is not open, so a PO cannot be attached to it."


def attach_po(  # type: ignore[no-untyped-def]
    project: Project,
    *,
    po_number: str,
    po_issue_date: date,
    contract_value: Decimal,
    cost_budget: Decimal,
    payment_terms: str = "",
    payment_terms_days: int | None = None,
    manager=None,
    actor,
    request=None,
) -> Project:
    """Set the PO on the **same row** and seed its milestones.

    Nothing is back-filled: entries recorded before the PO have no over-budget
    reason and are not re-checked (§4.19.8).
    """
    with transaction.atomic():
        project = Project.objects.select_for_update(of=("self",)).select_related("manager").get(
            pk=project.pk
        )
        if project.po_number:
            raise ProjectAlreadyHasPo()
        if project.status != ProjectStatus.OPEN:
            raise ProjectNotOpen()

        errors: dict[str, list[str]] = {}
        po_number = (po_number or "").strip()
        if not po_number:
            errors["po_number"] = ["Enter the PO number."]
        elif Project.objects.filter(po_number=po_number).exclude(pk=project.pk).exists():
            errors["po_number"] = ["Another project already carries this PO number (D21)."]
        chosen = manager or project.manager
        if chosen is None:
            errors["manager"] = ["A PO needs a project manager. Choose one."]
        elif not chosen.is_active:
            errors["manager"] = ["This person's account is not active."]
        if errors:
            raise ValidationError(errors)

        old_manager_id = project.manager_id
        before = {"po_number": "", "manager": old_manager_id}
        project.po_number = po_number
        project.po_issue_date = po_issue_date
        project.contract_value = contract_value
        project.cost_budget = cost_budget
        project.payment_terms = payment_terms
        project.payment_terms_days = payment_terms_days
        project.manager = chosen
        project.po_recorded_at = timezone.now()
        try:
            with transaction.atomic():
                project.save()
        except IntegrityError as exc:
            # Lost a race for the number between the check and the save.
            raise ValidationError(
                {"po_number": ["Another project already carries this PO number (D21)."]}
            ) from exc

        seed_default_milestones(project)
        assert chosen is not None  # the checks above refused a project with no manager
        if old_manager_id is not None and chosen.pk != old_manager_id:
            from approvals.engine import readdress_project_requests

            readdress_project_requests(
                project, old_manager_id=old_manager_id, actor=actor, request=request
            )
        # R12: Finance is told the PO arrived, so it can fill in the milestones.
        emit_po_attached(
            project,
            days_without_po=(project.po_recorded_at - project.opened_at).days,
            actor=actor,
        )
        record(
            AuditAction.DOCUMENT_AMENDED,
            actor=actor,
            target=project,
            request=request,
            before=before,
            after={
                "po_number": po_number,
                "po_issue_date": str(po_issue_date),
                "contract_value": str(contract_value),
                "cost_budget": str(cost_budget),
                "payment_terms_days": payment_terms_days,
                "manager": chosen.pk if chosen else None,
            },
            note=(
                f"PO {po_number} attached to {project.reference} "
                f"after {(project.po_recorded_at - project.opened_at).days} days."
            ),
        )
    return project
