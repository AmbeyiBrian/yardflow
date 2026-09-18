"""T10.16 — the manager on a project write-off (§5.4; O10).

The contrast with a gate pass is the whole task. On a pass the manager
**replaces** the criticality rules (D22) — a control deliberately traded away,
recorded as R2. On a disposal the manager is **added above** them, because a
write-off is permanent and there is no case for giving up a control that already
exists to get one that did not.
"""

from decimal import Decimal

import pytest

from accounts.factories import RoleFactory, UserFactory
from approvals.engine import required_levels
from approvals.models import ApprovalRule
from catalogue.factories import ItemTypeFactory
from catalogue.models import Criticality
from disposition.models import Disposal, DisposalMethod
from locations.factories import YardFactory
from network.factories import ProjectFactory


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def manager(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def owner_role(tenant):
    return RoleFactory(name="Owner")


@pytest.fixture
def high_rule(tenant, owner_role):
    return ApprovalRule.objects.create(
        organization=tenant,
        criticality=Criticality.HIGH,
        required_role=owner_role,
        sequence=9,
    )


@pytest.fixture
def po_project(tenant, manager):
    return ProjectFactory(
        reference="WO-9701",
        po_number="PO-970",
        manager=manager,
        contract_value=Decimal("100000.00"),
        cost_budget=Decimal("80000.00"),
    )


def disposal(tenant, yard, requester, project=None):
    return Disposal.objects.create(
        organization=tenant,
        method=DisposalMethod.SCRAP_DEALER,
        from_location=yard,
        requested_by=requester,
        project=project,
    )


def add_high_criticality_line(tenant, document):
    from disposition.models import DisposalLine

    category = ItemTypeFactory().category
    category.criticality = Criticality.HIGH
    category.save()
    item = ItemTypeFactory(category=category)
    DisposalLine.objects.create(
        organization=tenant,
        disposal=document,
        item_type=item,
        quantity=Decimal("1"),
        uom=item.uom,
    )


@pytest.mark.django_db
class TestTheManagerIsAddedNotSubstituted:
    def test_a_project_disposal_needs_the_manager_then_the_approver(
        self, tenant, yard, manager, po_project, high_rule, owner_role
    ):
        document = disposal(tenant, yard, manager, project=po_project)
        add_high_criticality_line(tenant, document)

        levels = required_levels(document)

        assert [level.user for level in levels] == [manager, None]
        assert [level.role for level in levels] == [None, owner_role]

    def test_the_manager_answers_first(
        self, tenant, yard, manager, po_project, high_rule
    ):
        """Levels are answered in order, so the budget holder sees it first."""
        document = disposal(tenant, yard, manager, project=po_project)
        add_high_criticality_line(tenant, document)

        levels = required_levels(document)

        assert levels[0].user == manager
        assert levels[0].level < levels[1].level

    def test_without_a_project_the_rules_are_unchanged(
        self, tenant, yard, manager, high_rule, owner_role
    ):
        document = disposal(tenant, yard, manager, project=None)
        add_high_criticality_line(tenant, document)

        levels = required_levels(document)

        assert [level.role for level in levels] == [owner_role]

    def test_a_project_disposal_with_no_matching_rule_still_needs_the_manager(
        self, tenant, yard, manager, po_project
    ):
        """No criticality rule applies, but the cost still lands on a budget."""
        document = disposal(tenant, yard, manager, project=po_project)

        levels = required_levels(document)

        assert [level.user for level in levels] == [manager]
