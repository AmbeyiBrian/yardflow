"""T10.2 — a project's purchase order fields (§4.14; O1, D20, D21, D24).

The rule these tests exist for is the check constraint. A project with a PO
number and no manager is one nobody can release material against (`O6`), and it
would not be discovered until a storekeeper tried — so it is refused by the
database, not by a form that a script or a shell can walk past.

The other half is D20's promise: a project *without* a PO number is the work
order this model used to be, and everything that worked before must still work
with no new field supplied.
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import UserFactory
from core.rls import rls_bypass
from network.factories import ProjectFactory
from network.models import Project, ProjectStatus


@pytest.mark.django_db
class TestAProjectWithoutAPurchaseOrder:
    """D20: the unpriced project is the old work order, unchanged."""

    def test_it_needs_no_commercial_fields_at_all(self, tenant):
        project = ProjectFactory()

        assert project.po_number == ""
        assert project.manager is None
        assert project.contract_value is None
        assert project.cost_budget is None
        assert project.is_po is False

    def test_many_of_them_coexist(self, tenant):
        """The PO uniqueness constraint is partial, so blanks do not collide."""
        ProjectFactory(reference="WO-3001")
        ProjectFactory(reference="WO-3002")

        assert Project.objects.filter(po_number="").count() == 2


@pytest.mark.django_db
class TestAProjectWithAPurchaseOrder:
    def test_it_is_accepted_when_fully_specified(self, tenant):
        project = ProjectFactory(
            po_number="PO-77",
            manager=UserFactory(),
            contract_value=Decimal("1200000.00"),
            cost_budget=Decimal("900000.00"),
        )

        assert project.is_po is True
        assert str(project) == "PO-77"

    @pytest.mark.parametrize("missing", ["manager", "contract_value", "cost_budget"])
    def test_it_is_refused_by_the_database_when_incomplete(self, tenant, missing):
        """O1: refused here, not at the gate with a van waiting."""
        fields = {
            "po_number": "PO-78",
            "manager": UserFactory(),
            "contract_value": Decimal("10.00"),
            "cost_budget": Decimal("5.00"),
        }
        fields[missing] = None

        with pytest.raises(IntegrityError), transaction.atomic():
            ProjectFactory(**fields)

    def test_a_po_number_identifies_one_project_only(self, tenant):
        """D21: one PO is one project. Growth is a variation, not a second row."""
        common = {
            "manager": UserFactory(),
            "contract_value": Decimal("1.00"),
            "cost_budget": Decimal("1.00"),
        }
        ProjectFactory(reference="WO-4001", po_number="PO-79", **common)

        with pytest.raises(IntegrityError), transaction.atomic():
            ProjectFactory(reference="WO-4002", po_number="PO-79", **common)

    def test_the_same_po_number_may_exist_in_another_tenant(
        self, organization, other_organization
    ):
        """A2: two subcontractors may each hold the same operator's PO number."""
        from core.tenancy import tenant_context

        for index, org in enumerate((organization, other_organization)):
            with tenant_context(org):
                ProjectFactory(
                    reference=f"WO-500{index}",
                    po_number="PO-SHARED",
                    manager=UserFactory(),
                    contract_value=Decimal("1.00"),
                    cost_budget=Decimal("1.00"),
                )

        # `all_objects` lifts the manager's filter; RLS is a separate barrier
        # and crossing it needs saying so (§2.3).
        with rls_bypass():
            assert Project.all_objects.filter(po_number="PO-SHARED").count() == 2


@pytest.mark.django_db
class TestProjectStatus:
    def test_a_cancelled_project_must_record_when(self, tenant):
        """A withdrawn PO is not a delivered one, but both have an end date."""
        project = ProjectFactory()
        project.status = ProjectStatus.CANCELLED

        with pytest.raises(IntegrityError), transaction.atomic():
            project.save()

    def test_cancelling_with_a_timestamp_is_accepted(self, tenant):
        project = ProjectFactory()
        project.status = ProjectStatus.CANCELLED
        project.closed_at = timezone.now()
        project.save()

        project.refresh_from_db()
        assert project.status == ProjectStatus.CANCELLED


@pytest.mark.django_db
class TestFindingAProject:
    """A person searches with the reference they happen to know (§6).

    Usually the client's PO number, which is the one on the paperwork in front
    of them — and which the search did not cover until the list started showing
    it alongside ours.
    """

    def test_it_is_found_by_the_po_number(self, tenant):
        from network.views import ProjectViewSet

        assert "po_number" in ProjectViewSet.search_fields

    def test_and_by_our_own_reference(self, tenant):
        from network.views import ProjectViewSet

        assert "reference" in ProjectViewSet.search_fields

    def test_and_by_title(self, tenant):
        from network.views import ProjectViewSet

        assert "title" in ProjectViewSet.search_fields
