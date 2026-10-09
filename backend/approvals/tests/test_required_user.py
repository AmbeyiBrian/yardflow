"""T10.8 — an approval addressed to a person (§5.4; O6).

Every level before this one routed to a **role**. Project material routes to
the project's manager, who is a particular person, and a rule table keyed on
category and criticality cannot say "the manager of whichever project this is
for" without inventing a placeholder role — which would then be grantable to
anybody, quietly undoing the control it was standing in for.

No routing change here. This task only makes the column exist, so that a
failure in T10.9 is unambiguously about routing.
"""

import pytest
from django.db import IntegrityError, transaction

from accounts.factories import RoleFactory, UserFactory
from approvals.models import ApprovalRequest


@pytest.mark.django_db
class TestARequestIsAddressedOnce:
    def test_a_role_alone_is_accepted(self, tenant):
        request = ApprovalRequest.objects.create(
            organization=tenant,
            document_type="dispatch.GateOut",
            document_id="1",
            required_role=RoleFactory(),
        )

        assert request.required_user is None

    def test_a_person_alone_is_accepted(self, tenant):
        request = ApprovalRequest.objects.create(
            organization=tenant,
            document_type="dispatch.GateOut",
            document_id="2",
            required_user=UserFactory(),
        )

        assert request.required_role is None

    def test_neither_is_accepted(self, tenant):
        """§5.2's auto-approval row: nobody was asked, and the trail says so."""
        request = ApprovalRequest.objects.create(
            organization=tenant,
            document_type="dispatch.GateOut",
            document_id="3",
        )

        assert request.required_role is None
        assert request.required_user is None

    def test_both_is_refused(self, tenant):
        with pytest.raises(IntegrityError), transaction.atomic():
            ApprovalRequest.objects.create(
                organization=tenant,
                document_type="dispatch.GateOut",
                document_id="4",
                required_role=RoleFactory(),
                required_user=UserFactory(),
            )


@pytest.mark.django_db
class TestAtMostOneOfThree:
    """R4: a level may also be addressed to whoever holds a permission."""

    def make(self, number, **fields):
        return ApprovalRequest.objects.create(
            organization=fields.pop("organization"),
            document_type="commercials.ProjectExpense",
            document_id=str(number),
            **fields,
        )

    def test_a_permission_alone_is_accepted(self, tenant):
        request = self.make(1, organization=tenant, required_permission="finance.approve")

        assert request.required_role is None
        assert request.required_user is None

    def test_a_permission_and_a_person_is_refused(self, tenant):
        with pytest.raises(IntegrityError), transaction.atomic():
            self.make(
                2,
                organization=tenant,
                required_permission="finance.approve",
                required_user=UserFactory(),
            )

    def test_a_permission_and_a_role_is_refused(self, tenant):
        with pytest.raises(IntegrityError), transaction.atomic():
            self.make(
                3,
                organization=tenant,
                required_permission="finance.approve",
                required_role=RoleFactory(),
            )
