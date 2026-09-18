"""T10.9 — routing project material to its manager (§5.4; O6, D22, D28).

D22 replaces the criticality rules for project material rather than adding to
them. That is a real weakening of a control that exists elsewhere in this
system, taken deliberately, and R2 in the requirements is where its cost is
written down. These tests pin the shape of what was traded:

- the PM decides, and **nobody else** — not a delegate, not a blanket
  permission holder, not the owner;
- a PM may approve their own request, and it is **recorded**;
- an inactive PM **stops the project**, rather than quietly falling back to a
  weaker rule at the one moment nobody is watching.
"""

from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory
from approvals.engine import (
    ProjectHasNoActiveManager,
    can_approve,
    create_requests,
    required_levels,
)
from approvals.models import ApprovalRule
from catalogue.models import Criticality
from dispatch.models import GateOut, GateOutPurpose
from jobs.models import Job
from locations.factories import YardFactory
from network.factories import ProjectFactory, SiteFactory


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def manager(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def technician(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def site(tenant):
    return SiteFactory(internal_ref="SLV-4001", name="Kileleshwa")


@pytest.fixture
def po_project(tenant, manager):
    return ProjectFactory(
        reference="WO-8001",
        po_number="PO-800",
        manager=manager,
        contract_value=Decimal("100000.00"),
        cost_budget=Decimal("80000.00"),
    )


@pytest.fixture
def job(tenant, site, technician, po_project):
    return Job.objects.create(
        organization=tenant,
        reference="JOB-R01",
        client=site.client,
        site=site,
        project=po_project,
        assignee=technician,
    )


@pytest.fixture
def owner_rule(tenant):
    """A criticality rule that would fire on anything, if it were reached."""
    return ApprovalRule.objects.create(
        organization=tenant,
        criticality=Criticality.HIGH,
        required_role=RoleFactory(name="Owner"),
        sequence=9,
    )


def project_pass(tenant, yard, technician, site, job, **kwargs):
    gate_out = GateOut(
        organization=tenant,
        purpose_type=GateOutPurpose.INSTALLATION,
        from_location=yard,
        site=site,
        job=job,
        custody_holder=technician,
        requested_by=technician,
        **kwargs,
    )
    gate_out.save()
    return gate_out


@pytest.mark.django_db
class TestRouting:
    def test_project_material_routes_to_the_manager(
        self, tenant, yard, technician, site, job, manager
    ):
        levels = required_levels(project_pass(tenant, yard, technician, site, job))

        assert len(levels) == 1
        assert levels[0].user == manager
        assert levels[0].role is None

    def test_it_never_reaches_a_criticality_rule(
        self, tenant, yard, technician, site, job, manager, owner_rule
    ):
        """D22: the criticality path is not consulted for project material."""
        levels = required_levels(project_pass(tenant, yard, technician, site, job))

        assert [level.user for level in levels] == [manager]
        assert owner_rule.required_role not in [level.role for level in levels]

    def test_a_pass_with_no_project_still_uses_the_rules(
        self, tenant, yard, technician, site, job
    ):
        """Non-project material is untouched by any of this."""
        job.project = None
        job.save()
        levels = required_levels(project_pass(tenant, yard, technician, site, job))

        assert [level.user for level in levels] == []

    def test_an_unpriced_project_with_no_manager_uses_the_rules(
        self, tenant, yard, technician, site, job
    ):
        """A project without a PO is the old work order, and has no manager."""
        job.project = ProjectFactory(reference="WO-8002")
        job.save()
        levels = required_levels(project_pass(tenant, yard, technician, site, job))

        assert [level.user for level in levels] == []


@pytest.mark.django_db
class TestTheRequestItCreates:
    def test_it_is_addressed_to_the_manager_with_no_escalation_clock(
        self, tenant, yard, technician, site, job, manager
    ):
        """D22: no escalation. A null `due_at` is what the sweep skips on."""
        gate_out = project_pass(tenant, yard, technician, site, job)
        requests = create_requests(gate_out, requested_by=technician)

        assert len(requests) == 1
        assert requests[0].required_user == manager
        assert requests[0].required_role is None
        assert requests[0].due_at is None


@pytest.mark.django_db
class TestWhoMayDecide:
    def test_the_manager_may(self, tenant, yard, technician, site, job, manager):
        gate_out = project_pass(tenant, yard, technician, site, job)
        request = create_requests(gate_out, requested_by=technician)[0]

        allowed, _ = can_approve(manager, request, document=gate_out)
        assert allowed is True

    def test_nobody_else_may_even_holding_every_permission(
        self, tenant, yard, technician, site, job
    ):
        """No blanket-permission override on a person-addressed level."""
        from accounts.permissions_registry import ALL_CODENAMES

        gate_out = project_pass(tenant, yard, technician, site, job)
        request = create_requests(gate_out, requested_by=technician)[0]

        owner = UserFactory(organization=tenant, full_name="Olive Owner")
        owner.user_roles.create(
            organization=tenant, role=RoleFactory(codenames=sorted(ALL_CODENAMES))
        )

        allowed, why = can_approve(owner, request, document=gate_out)
        assert allowed is False
        assert why == "not_the_manager"

    def test_a_delegate_may_not(self, tenant, yard, technician, site, job, manager):
        """A delegation lends a role. Lending someone's signature on a budget
        they are accountable for is not the same thing (D22)."""
        gate_out = project_pass(tenant, yard, technician, site, job)
        request = create_requests(gate_out, requested_by=technician)[0]

        stand_in = UserFactory(organization=tenant, full_name="Stan Standin")
        role = RoleFactory(name="Approver")
        stand_in.delegations_received.create(
            organization=tenant,
            from_user=manager,
            role=role,
            starts_at=timezone.now() - timezone.timedelta(days=1),
            ends_at=timezone.now() + timezone.timedelta(days=1),
        )

        allowed, why = can_approve(stand_in, request, document=gate_out)
        assert allowed is False
        assert why == "not_the_manager"

    def test_the_manager_may_approve_their_own_request(
        self, tenant, yard, site, job, manager
    ):
        """O6: permitted, and R2 is where the cost of permitting it is written."""
        gate_out = project_pass(tenant, yard, manager, site, job)
        request = create_requests(gate_out, requested_by=manager)[0]

        allowed, why = can_approve(manager, request, document=gate_out)
        assert allowed is True
        assert why == "self"


@pytest.mark.django_db
class TestAnInactiveManagerStopsTheProject:
    def test_routing_refuses_rather_than_falling_back(
        self, tenant, yard, technician, site, job, manager, owner_rule
    ):
        """D28: falling through would restore a weaker control silently."""
        manager.is_active = False
        manager.save()

        gate_out = project_pass(tenant, yard, technician, site, job)
        with pytest.raises(ProjectHasNoActiveManager, match="assigns a new manager"):
            required_levels(gate_out)
