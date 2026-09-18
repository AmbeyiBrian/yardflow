"""T10.3 — variations to a project's value and budget (§4.14; O2, D21).

D21 is the whole of it: **the original award is never edited.** A dispute with
an operator is usually about what was first agreed, and a column overwritten
three times cannot answer that. So the current value is a sum, the original
column is written once, and a decided variation cannot be touched again.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import UserFactory
from network.factories import ProjectFactory
from network.models import ProjectVariation, VariationStatus


@pytest.fixture
def po_project(tenant):
    return ProjectFactory(
        reference="WO-9001",
        po_number="PO-900",
        manager=UserFactory(),
        contract_value=Decimal("1000000.00"),
        cost_budget=Decimal("800000.00"),
    )


def raise_variation(project, **kwargs):
    defaults = {
        "organization": project.organization,
        "project": project,
        "reference": "VO-1",
        "value_delta": Decimal("0"),
        "budget_delta": Decimal("0"),
        "effective_on": date(2026, 3, 1),
        "raised_by": UserFactory(),
    }
    return ProjectVariation.objects.create(**{**defaults, **kwargs})


def approve(variation):
    variation.status = VariationStatus.APPROVED
    variation.decided_by = UserFactory()
    variation.decided_at = timezone.now()
    variation.save()
    return variation


@pytest.mark.django_db
class TestTheOriginalAwardIsNeverEdited:
    def test_an_approved_variation_moves_the_current_value_only(self, po_project):
        approve(
            raise_variation(
                po_project, value_delta=Decimal("250000.00"), budget_delta=Decimal("150000.00")
            )
        )
        po_project.refresh_from_db()

        assert po_project.contract_value == Decimal("1000000.00")
        assert po_project.current_contract_value == Decimal("1250000.00")
        assert po_project.cost_budget == Decimal("800000.00")
        assert po_project.current_cost_budget == Decimal("950000.00")

    def test_a_descope_reduces_it(self, po_project):
        """Scope gets taken away too, and a negative delta says so plainly."""
        approve(raise_variation(po_project, value_delta=Decimal("-100000.00")))

        assert po_project.current_contract_value == Decimal("900000.00")

    def test_variations_accumulate(self, po_project):
        approve(raise_variation(po_project, reference="VO-1", value_delta=Decimal("100.00")))
        approve(raise_variation(po_project, reference="VO-2", value_delta=Decimal("50.00")))

        assert po_project.current_contract_value == Decimal("1000150.00")

    def test_a_pending_variation_moves_nothing(self, po_project):
        """It is a request until the owner rules on it (O2)."""
        raise_variation(po_project, value_delta=Decimal("999.00"))

        assert po_project.current_contract_value == Decimal("1000000.00")

    def test_a_rejected_variation_moves_nothing(self, po_project):
        variation = raise_variation(po_project, value_delta=Decimal("999.00"))
        variation.status = VariationStatus.REJECTED
        variation.decided_by = UserFactory()
        variation.decided_at = timezone.now()
        variation.decision_reason = "Not agreed with the operator."
        variation.save()

        assert po_project.current_contract_value == Decimal("1000000.00")

    def test_a_project_with_no_value_has_no_current_value(self, tenant):
        """An unpriced project is the old work order and has nothing to vary."""
        assert ProjectFactory().current_contract_value is None


@pytest.mark.django_db
class TestADecidedVariationIsFinal:
    def test_it_cannot_be_edited(self, po_project):
        variation = approve(raise_variation(po_project, value_delta=Decimal("100.00")))

        variation.value_delta = Decimal("999999.00")
        with pytest.raises(ValidationError, match="cannot be changed"):
            variation.save()

    def test_a_rejected_one_cannot_be_revived(self, po_project):
        variation = raise_variation(po_project)
        variation.status = VariationStatus.REJECTED
        variation.decided_by = UserFactory()
        variation.decided_at = timezone.now()
        variation.save()

        variation.status = VariationStatus.APPROVED
        with pytest.raises(ValidationError, match="cannot be changed"):
            variation.save()

    def test_a_pending_one_may_still_be_corrected(self, po_project):
        """Nothing has been agreed yet, so nothing is being rewritten."""
        variation = raise_variation(po_project, value_delta=Decimal("100.00"))
        variation.value_delta = Decimal("200.00")
        variation.save()

        variation.refresh_from_db()
        assert variation.value_delta == Decimal("200.00")

    def test_it_is_never_deleted(self, po_project):
        variation = approve(raise_variation(po_project))

        with pytest.raises(ValidationError, match="never deleted"):
            variation.delete()

    def test_a_decision_must_record_when(self, po_project):
        variation = raise_variation(po_project)
        variation.status = VariationStatus.APPROVED

        with pytest.raises(IntegrityError), transaction.atomic():
            variation.save()

    def test_a_reference_is_unique_within_a_project(self, po_project):
        raise_variation(po_project, reference="VO-7")

        with pytest.raises(IntegrityError), transaction.atomic():
            raise_variation(po_project, reference="VO-7")
