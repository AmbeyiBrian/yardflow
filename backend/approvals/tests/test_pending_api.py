"""T15.2 — ``/approvals/pending`` shows a caller only their own levels (§4.17.6).

It used to pass every ``required_role IS NULL`` row, so every person-addressed
PM request showed to everyone. These tests pin the fix, and that a gate-out PM
approval (addressed to the PM by name, O6) still shows to its PM and to nobody
else.

Also here: reassigning a project's manager re-addresses what was waiting on the
old one (R4, D28), because the same endpoint is what the new PM then reads.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.models import Role, User, UserRole
from accounts.permissions_registry import PERM
from approvals.engine import create_requests
from approvals.models import ApprovalRequest
from commercials.models import (
    AllowanceRequest,
    AllowanceType,
    ExpenseCategory,
    ProjectExpense,
)
from core.models import AuditAction, AuditLog
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from network.factories import ProjectFactory

PASSWORD = "a good long password"


@pytest.fixture
def world(db, client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    owner = result["owner"]
    owner.set_password(PASSWORD)
    owner.save()
    client.defaults["HTTP_HOST"] = "silvertech.localhost"
    return client, result["organization"], owner


def sign_in(client, email):
    return {
        "HTTP_AUTHORIZATION": "Bearer "
        + client.post(
            reverse("v1:auth:login"),
            {"identifier": email, "password": PASSWORD},
            content_type="application/json",
        ).json()["access"]
    }


def make_user(organization, email, *, codenames=()):
    user = User.objects.create_user(
        email=email, password=PASSWORD, organization=organization, full_name=email
    )
    if codenames:
        role = Role.objects.create(organization=organization, name=f"Role {email}")
        role.set_permissions(list(codenames))
        UserRole.objects.create(organization=organization, user=user, role=role)
    return user


@pytest.fixture
def cast(world):
    """Two PMs, a recorder, a Finance user, and one expense and one gate-out request."""
    _client, organization, owner = world
    with tenant_context(organization):
        pm = make_user(organization, "pm@silvertech.co.ke")
        other_pm = make_user(organization, "other-pm@silvertech.co.ke")
        recorder = make_user(organization, "tech@silvertech.co.ke")
        finance = make_user(
            organization, "fin@silvertech.co.ke", codenames=[PERM.FINANCE_APPROVE]
        )
        project = ProjectFactory(
            reference="WO-9901",
            po_number="PO-990",
            manager=pm,
            contract_value=Decimal("100000.00"),
            cost_budget=Decimal("80000.00"),
        )
        category = ExpenseCategory.objects.create(organization=organization, name="Misc")
        expense = ProjectExpense.objects.create(
            organization=organization,
            project=project,
            category=category,
            amount=Decimal("1200.00"),
            incurred_on=date(2026, 10, 1),
            description="Fuel to site",
            recorded_by=recorder,
        )
        create_requests(expense, requested_by=recorder)
        # A gate-out PM approval, addressed to the PM by name (O6). The document
        # row itself is not needed: the list falls back to its number.
        gate_out_request = ApprovalRequest.objects.create(
            organization=organization,
            document_type="dispatch.GateOut",
            document_id="999999",
            document_number="GP-000099",
            level=1,
            required_user=pm,
        )
        return {
            "organization": organization,
            "owner": owner,
            "pm": pm,
            "other_pm": other_pm,
            "recorder": recorder,
            "finance": finance,
            "project": project,
            "expense": expense,
            "gate_out_request": gate_out_request,
        }


def pending_for(client, email):
    response = client.get(reverse("v1:approval-pending"), **sign_in(client, email))
    assert response.status_code == 200, response.content
    body = response.json()
    rows = body["results"] if isinstance(body, dict) else body
    return [(row["document_type"], row["level"]) for row in rows], rows


class TestPendingIsOnlyYours:
    def test_the_pm_sees_their_own_levels_only(self, world, cast):
        client, *_ = world

        pairs, _rows = pending_for(client, "pm@silvertech.co.ke")

        assert sorted(pairs) == [
            ("commercials.ProjectExpense", 1),
            ("dispatch.GateOut", 1),
        ]

    def test_an_unrelated_member_sees_nothing(self, world, cast):
        """The leak: a person-addressed request used to show to everyone."""
        client, *_ = world

        assert pending_for(client, "other-pm@silvertech.co.ke")[0] == []
        assert pending_for(client, "tech@silvertech.co.ke")[0] == []

    def test_finance_does_not_see_level_two_until_the_pm_has_answered(self, world, cast):
        client, *_ = world
        assert pending_for(client, "fin@silvertech.co.ke")[0] == []

        with tenant_context(cast["organization"]):
            from approvals.engine import record_decision
            from approvals.models import ApprovalDecision

            record_decision(
                cast["expense"], actor=cast["pm"], decision=ApprovalDecision.APPROVED
            )

        assert pending_for(client, "fin@silvertech.co.ke")[0] == [
            ("commercials.ProjectExpense", 2)
        ]
        # ...and the PM's entry has left the PM's list, the gate-out has not.
        assert pending_for(client, "pm@silvertech.co.ke")[0] == [("dispatch.GateOut", 1)]

    def test_the_gate_out_pm_request_shows_to_no_one_else(self, world, cast):
        client, *_ = world

        for email in ("fin@silvertech.co.ke", "tech@silvertech.co.ke"):
            assert ("dispatch.GateOut", 1) not in pending_for(client, email)[0]

    def test_the_expense_is_summarised_for_the_approver(self, world, cast):
        client, *_ = world

        _pairs, rows = pending_for(client, "pm@silvertech.co.ke")

        document = next(
            row["document"] for row in rows if row["document_type"].endswith("ProjectExpense")
        )
        assert document["amount"] == "1200.00"
        assert document["type"] == "Misc"
        assert document["description"] == "Fuel to site"
        assert document["requested_by_id"] == cast["recorder"].pk
        assert document["project"] == str(cast["project"])
        assert document["evidence"] == "NONE"

    def test_an_allowance_is_summarised_too(self, world, cast):
        client, organization, _owner = world
        with tenant_context(organization):
            allowance = AllowanceRequest.objects.create(
                organization=organization,
                number="AR-000001",
                type=AllowanceType.TEAM_ALLOWANCE,
                amount=Decimal("3000.00"),
                from_date=date(2026, 10, 1),
                to_date=date(2026, 10, 2),
                project=cast["project"],
                recorded_by=cast["recorder"],
                reason="Two nights",
            )
            create_requests(allowance, requested_by=cast["recorder"])

        _pairs, rows = pending_for(client, "pm@silvertech.co.ke")

        document = next(
            row["document"] for row in rows if row["document_type"].endswith("AllowanceRequest")
        )
        assert document["number"] == "AR-000001"
        assert document["amount"] == "3000.00"
        assert document["type"] == "Team allowance"
        assert document["description"] == "Two nights"


class TestReassigningTheManager:
    def patch_manager(self, client, project, manager):
        return client.patch(
            reverse("v1:project-detail", args=[project.pk]),
            {"manager": manager.pk},
            content_type="application/json",
            **sign_in(client, "owner@silvertech.co.ke"),
        )

    def test_open_pm_levels_move_to_the_new_manager(self, world, cast):
        client, organization, _owner = world

        response = self.patch_manager(client, cast["project"], cast["other_pm"])

        assert response.status_code == 200, response.content
        with tenant_context(organization):
            levels = {
                (r.document_type, r.level): r for r in ApprovalRequest.objects.all()
            }
            assert levels[("commercials.ProjectExpense", 1)].required_user == cast["other_pm"]
            # The O6 gate-out level is the same concept, but this request's
            # document does not exist, so it cannot be tied to the project and
            # stays put.
            assert levels[("dispatch.GateOut", 1)].required_user == cast["pm"]
            # Finance is addressed to a permission and is never touched.
            finance_level = levels[("commercials.ProjectExpense", 2)]
            assert finance_level.required_user is None
            assert finance_level.required_permission == "finance.approve"

        assert pending_for(client, "other-pm@silvertech.co.ke")[0] == [
            ("commercials.ProjectExpense", 1)
        ]
        assert ("commercials.ProjectExpense", 1) not in pending_for(
            client, "pm@silvertech.co.ke"
        )[0]

    def test_a_gate_out_pm_level_on_the_project_moves_too(self, world, cast):
        """O6: the same concept, the project manager's signature."""
        client, organization, _owner = world
        with tenant_context(organization):
            from dispatch.models import GateOut, GateOutPurpose
            from jobs.models import Job
            from locations.factories import YardFactory
            from network.factories import SiteFactory

            site = SiteFactory(internal_ref="SLV-9901", name="Kileleshwa")
            job = Job.objects.create(
                organization=organization,
                reference="JOB-R99",
                client=site.client,
                site=site,
                project=cast["project"],
                assignee=cast["recorder"],
            )
            gate_out = GateOut(
                organization=organization,
                purpose_type=GateOutPurpose.INSTALLATION,
                from_location=YardFactory(name="Main yard"),
                site=site,
                job=job,
                custody_holder=cast["recorder"],
                requested_by=cast["recorder"],
            )
            gate_out.save()
            (request_row,) = create_requests(gate_out, requested_by=cast["recorder"])
            assert request_row.required_user == cast["pm"]

        self.patch_manager(client, cast["project"], cast["other_pm"])

        with tenant_context(organization):
            request_row.refresh_from_db()
            assert request_row.required_user == cast["other_pm"]

    def test_it_is_audited(self, world, cast):
        client, organization, _owner = world

        self.patch_manager(client, cast["project"], cast["other_pm"])

        with tenant_context(organization):
            entry = AuditLog.objects.filter(
                action=AuditAction.STATUS_CHANGED, target_id=str(cast["project"].pk)
            ).get()
            assert "re-addressed" in entry.note

    def test_decided_requests_are_left_alone(self, world, cast):
        client, organization, _owner = world
        with tenant_context(organization):
            from approvals.engine import record_decision
            from approvals.models import ApprovalDecision

            record_decision(
                cast["expense"], actor=cast["pm"], decision=ApprovalDecision.APPROVED
            )

        self.patch_manager(client, cast["project"], cast["other_pm"])

        with tenant_context(organization):
            decided = ApprovalRequest.objects.get(
                document_type="commercials.ProjectExpense", level=1
            )
            assert decided.required_user == cast["pm"]

    def test_another_projects_requests_are_left_alone(self, world, cast):
        client, organization, _owner = world
        with tenant_context(organization):
            elsewhere = ProjectFactory(
                reference="WO-9902",
                po_number="PO-991",
                manager=cast["pm"],
                contract_value=Decimal("1000.00"),
                cost_budget=Decimal("800.00"),
            )
            other_expense = ProjectExpense.objects.create(
                organization=organization,
                project=elsewhere,
                category=ExpenseCategory.objects.get(name="Misc"),
                amount=Decimal("10.00"),
                incurred_on=date(2026, 10, 2),
                recorded_by=cast["recorder"],
            )
            create_requests(other_expense, requested_by=cast["recorder"])

        self.patch_manager(client, cast["project"], cast["other_pm"])

        with tenant_context(organization):
            untouched = ApprovalRequest.objects.get(
                document_type="commercials.ProjectExpense",
                document_id=str(other_expense.pk),
                level=1,
            )
            assert untouched.required_user == cast["pm"]

    def test_editing_something_else_does_nothing(self, world, cast):
        client, organization, _owner = world

        response = client.patch(
            reverse("v1:project-detail", args=[cast["project"].pk]),
            {"title": "Renamed"},
            content_type="application/json",
            **sign_in(client, "owner@silvertech.co.ke"),
        )

        assert response.status_code == 200, response.content
        with tenant_context(organization):
            assert not AuditLog.objects.filter(
                action=AuditAction.STATUS_CHANGED, note__contains="re-addressed"
            ).exists()
