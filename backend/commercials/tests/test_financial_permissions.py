"""T10.18 — who sees which figure (§10; O14).

O14 asks for these assertions **field by field**, and that phrasing is the
requirement: a test that only checks the happy path would pass just as happily
against a serializer that returned every number to everybody.

Two rules are being pinned:

1. A withheld figure is **absent**, not null and not zero. Null is a claim about
   the project ("there is no budget"); absent is a claim about the reader ("you
   were not told"), and only the second is true.
2. A manager sees cost on **their own** projects only — which no single
   permission can express, so it is checked per project.
"""

from decimal import Decimal

import pytest

from accounts.factories import RoleFactory, UserFactory
from accounts.permissions_registry import PERM
from commercials.costing import performance_for
from commercials.serializers import ProjectPerformanceSerializer
from network.factories import ProjectFactory
from network.views import ProjectSerializer

COST_FIELDS = ("cost_to_date", "material", "labour", "cost_budget", "exposure")
MARGIN_FIELDS = ("contract_value", "margin", "margin_percent")


def viewer_with(tenant, *codenames, name="Viewer"):
    user = UserFactory(organization=tenant, full_name=name)
    user.user_roles.create(
        organization=tenant, role=RoleFactory(name=name, codenames=list(codenames))
    )
    return user


@pytest.fixture
def manager(tenant):
    return viewer_with(tenant, PERM.PROJECT_VIEW_COST, name="Pippa Manager")


@pytest.fixture
def owner(tenant):
    return viewer_with(
        tenant,
        PERM.PROJECT_VIEW_COST,
        PERM.PROJECT_VIEW_MARGIN,
        PERM.PROJECT_VIEW_RATES,
        name="Olive Owner",
    )


@pytest.fixture
def storekeeper(tenant):
    return viewer_with(tenant, PERM.GATE_OUT_REQUEST, name="Sara Storekeeper")


@pytest.fixture
def project(tenant, manager):
    return ProjectFactory(
        reference="WO-9901",
        po_number="PO-990",
        manager=manager,
        contract_value=Decimal("500000.00"),
        cost_budget=Decimal("300000.00"),
    )


def performance_as(rf, viewer, project):
    request = rf.get("/api/v1/projects/1/performance")
    request.user = viewer
    return ProjectPerformanceSerializer(
        performance_for(project), context={"request": request}
    ).data


def project_as(rf, viewer, project):
    request = rf.get("/api/v1/projects")
    request.user = viewer
    return ProjectSerializer(project, context={"request": request}).data


@pytest.mark.django_db
class TestTheStorekeeperSeesNoMoney:
    def test_not_one_cost_field(self, tenant, storekeeper, project, rf):
        data = performance_as(rf, storekeeper, project)

        for name in COST_FIELDS:
            assert name not in data, f"{name} leaked to a storekeeper"

    def test_not_one_margin_field(self, tenant, storekeeper, project, rf):
        data = performance_as(rf, storekeeper, project)

        for name in MARGIN_FIELDS:
            assert name not in data, f"{name} leaked to a storekeeper"

    def test_the_project_record_carries_no_money_either(
        self, tenant, storekeeper, project, rf
    ):
        data = project_as(rf, storekeeper, project)

        for name in (
            "contract_value",
            "current_contract_value",
            "cost_budget",
            "current_cost_budget",
        ):
            assert name not in data, f"{name} leaked to a storekeeper"

    def test_they_still_see_the_project_itself(self, tenant, storekeeper, project, rf):
        """Withholding money must not withhold the work."""
        data = project_as(rf, storekeeper, project)

        assert data["reference"] == "WO-9901"
        assert data["po_number"] == "PO-990"


@pytest.mark.django_db
class TestTheManagerSeesCostButNotMargin:
    def test_cost_is_present(self, tenant, manager, project, rf):
        data = performance_as(rf, manager, project)

        for name in COST_FIELDS:
            assert name in data, f"{name} withheld from the manager"

    def test_margin_is_absent_not_null(self, tenant, manager, project, rf):
        """The distinction O14 turns on."""
        data = performance_as(rf, manager, project)

        for name in MARGIN_FIELDS:
            assert name not in data, f"{name} shown to the manager"

    def test_they_see_no_cost_on_somebody_elses_project(
        self, tenant, manager, owner, rf
    ):
        """The scoping a permission alone cannot express."""
        other = ProjectFactory(
            reference="WO-9902",
            po_number="PO-991",
            manager=owner,
            contract_value=Decimal("1.00"),
            cost_budget=Decimal("1.00"),
        )

        data = performance_as(rf, manager, other)

        for name in COST_FIELDS:
            assert name not in data, f"{name} leaked across projects"

    def test_progress_is_visible_either_way(self, tenant, manager, owner, rf):
        """Jobs closed over jobs opened is quantities, not money."""
        other = ProjectFactory(
            reference="WO-9903",
            po_number="PO-992",
            manager=owner,
            contract_value=Decimal("1.00"),
            cost_budget=Decimal("1.00"),
        )

        assert "jobs_total" in performance_as(rf, manager, other)


@pytest.mark.django_db
class TestTheOwnerSeesEverything:
    def test_every_figure_is_present(self, tenant, owner, project, rf):
        data = performance_as(rf, owner, project)

        for name in COST_FIELDS + MARGIN_FIELDS:
            assert name in data, f"{name} withheld from the owner"

    def test_on_a_project_they_do_not_manage(self, tenant, owner, project, rf):
        """They can see the contract value anyway, so scoping cost would be
        a restriction with nothing behind it."""
        assert project.manager != owner
        data = performance_as(rf, owner, project)

        assert "cost_to_date" in data
        assert "margin" in data


@pytest.mark.django_db
class TestOffRequestCallersAreNotStarved:
    def test_no_request_withholds_nothing(self, tenant, project):
        """An export has already passed whatever check applies to it, and
        silently emptying its output would only surface in a trusted figure."""
        data = ProjectPerformanceSerializer(performance_for(project)).data

        for name in COST_FIELDS + MARGIN_FIELDS:
            assert name in data
