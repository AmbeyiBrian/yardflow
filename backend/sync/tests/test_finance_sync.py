"""T15.6 — money entries replayed from a phone (R6, design §4.17.8, §8).

The queue is a second door to the same three services, so what is pinned here is
that it lands the same entry the online form would, once, and that a refusal is
a recorded, resolvable exception carrying the domain code rather than a lost
capture (§8.4). Approving and paying are never offline (§8.3).
"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from commercials import finance
from commercials.models import (
    AllowanceRequest,
    Casual,
    ExpenseCategory,
    ExpenseKind,
    ProjectExpense,
)
from commercials.tests.finance_helpers import approve_through
from network.factories import ProjectFactory, SiteFactory
from sync.models import ExceptionStatus, SubmissionStatus, SyncException, SyncSubmission
from sync.services import apply_submission, resolve_exception

pytestmark = pytest.mark.django_db

DAY = "2026-10-05"


@pytest.fixture
def finance_user(tenant):
    """Someone holding ``finance.approve``; recording refuses without one (R4)."""
    role = RoleFactory(name="Finance", codenames=[PERM.FINANCE_APPROVE])
    user = UserFactory(organization=tenant, full_name="Fiona Finance")
    UserRoleFactory(user=user, role=role)
    return user


@pytest.fixture
def pm(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager")


@pytest.fixture
def tech(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(reference="WO-7001", manager=pm)


@pytest.fixture
def site(tenant, project):
    site = SiteFactory(name="Ruiru")
    project.sites.add(site)
    return site


@pytest.fixture
def category(tenant):
    return ExpenseCategory.objects.create(organization=tenant, name="Misc")


@pytest.fixture
def labour(tenant):
    return ExpenseCategory.objects.create(
        organization=tenant, name="Casual labour", kind=ExpenseKind.CASUAL_LABOUR
    )


def send(tenant, tech, operation, payload, uuid=None):
    uuid = uuid or uuid4()
    return apply_submission(
        organization=tenant,
        client_uuid=uuid,
        operation=operation,
        payload={**payload, "client_uuid": str(uuid)},
        submitted_by=tech,
    )


def expense_body(category, site, **extra):
    return {
        "category": category.pk,
        "site": site.pk,
        "amount": "1500.00",
        "incurred_on": DAY,
        "description": "Bolts",
        **extra,
    }


def allowance_body(site, **extra):
    return {
        "type": "NIGHT_OUT",
        "amount": "2000",
        "from_date": DAY,
        "to_date": DAY,
        "site": site.pk,
        "reason": "Late job",
        **extra,
    }


class TestReplayOfEachKind:
    def test_an_expense_lands_as_the_online_form_would(
        self, tenant, tech, finance_user, site, category
    ):
        submission, replay = send(tenant, tech, "EXPENSE", expense_body(category, site))

        assert not replay
        assert submission.status == SubmissionStatus.APPLIED
        expense = ProjectExpense.objects.get()
        assert submission.document_id == str(expense.pk)
        assert submission.document_type == ProjectExpense._meta.label
        assert expense.recorded_by == tech
        assert expense.amount == Decimal("1500.00")
        assert expense.client_uuid == submission.client_uuid
        assert expense.status == "PENDING_PM"

    def test_an_allowance_request_lands(self, tenant, tech, finance_user, site):
        submission, _ = send(tenant, tech, "ALLOWANCE_REQUEST", allowance_body(site))

        assert submission.status == SubmissionStatus.APPLIED
        request = AllowanceRequest.objects.get()
        assert submission.document_id == str(request.pk)
        assert submission.document_number == request.number
        assert request.recorded_by == tech

    def test_a_casual_registers(self, tenant, tech):
        submission, _ = send(
            tenant, tech, "CASUAL", {"name": "John Kamau", "id_number": "12345678"}
        )

        assert submission.status == SubmissionStatus.APPLIED
        assert Casual.objects.get().pk == int(submission.document_id)


class TestIdempotency:
    def test_a_duplicate_returns_the_same_entity(self, tenant, tech, finance_user, site, category):
        uuid = uuid4()
        first, _ = send(tenant, tech, "EXPENSE", expense_body(category, site), uuid)
        second, replay = send(tenant, tech, "EXPENSE", expense_body(category, site), uuid)

        assert replay
        assert second.pk == first.pk
        assert second.document_id == first.document_id
        assert ProjectExpense.objects.count() == 1

    def test_an_entry_the_form_already_made_online_is_not_doubled(
        self, tenant, tech, finance_user, site, category
    ):
        # The form tried the server, lost the response, and queued the same uuid.
        uuid = uuid4()
        made = finance.record_expense(
            actor=tech,
            category=category,
            amount=Decimal("1500"),
            incurred_on=date(2026, 10, 5),
            site=site,
            client_uuid=uuid,
        )
        submission, _ = send(tenant, tech, "EXPENSE", expense_body(category, site), uuid)

        assert submission.document_id == str(made.pk)
        assert ProjectExpense.objects.count() == 1


class TestRefusals:
    def test_a_domain_refusal_is_an_exception_with_its_code(
        self, tenant, tech, finance_user, site
    ):
        send(tenant, tech, "ALLOWANCE_REQUEST", allowance_body(site))
        submission, _ = send(tenant, tech, "ALLOWANCE_REQUEST", allowance_body(site))

        assert submission.status == SubmissionStatus.REJECTED
        exception = SyncException.objects.get()
        assert exception.code == "ALLOWANCE_OVERLAP"
        assert exception.reason
        assert AllowanceRequest.objects.count() == 1

    def test_supersede_resolves_the_old_exception_once_the_new_one_lands(
        self, tenant, tech, finance_user, site
    ):
        send(tenant, tech, "ALLOWANCE_REQUEST", allowance_body(site))
        bad_uuid = uuid4()
        send(tenant, tech, "ALLOWANCE_REQUEST", allowance_body(site), bad_uuid)

        fixed, _ = send(
            tenant,
            tech,
            "ALLOWANCE_REQUEST",
            allowance_body(site, from_date="2026-10-20", to_date="2026-10-20",
                           supersedes_client_uuid=str(bad_uuid)),
        )

        assert fixed.status == SubmissionStatus.APPLIED
        old = SyncException.objects.get()
        assert old.status == ExceptionStatus.RESOLVED
        assert old.resolution == "Corrected and resent"
        assert old.resolved_by == tech
        assert old.replacement_submission == fixed

    def test_a_correction_that_is_refused_leaves_the_old_exception_open(
        self, tenant, tech, finance_user, site
    ):
        send(tenant, tech, "ALLOWANCE_REQUEST", allowance_body(site))
        bad_uuid = uuid4()
        send(tenant, tech, "ALLOWANCE_REQUEST", allowance_body(site), bad_uuid)

        send(
            tenant,
            tech,
            "ALLOWANCE_REQUEST",
            allowance_body(site, supersedes_client_uuid=str(bad_uuid)),
        )

        assert SyncException.objects.get(submission__client_uuid=bad_uuid).is_open

    def test_a_supersede_naming_nothing_is_harmless(self, tenant, tech, finance_user, site):
        submission, _ = send(
            tenant,
            tech,
            "ALLOWANCE_REQUEST",
            allowance_body(site, supersedes_client_uuid=str(uuid4())),
        )
        assert submission.status == SubmissionStatus.APPLIED

    def test_an_unparseable_amount_is_a_refusal_not_a_crash(
        self, tenant, tech, finance_user, site, category
    ):
        submission, _ = send(
            tenant, tech, "EXPENSE", expense_body(category, site, amount="lots")
        )
        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "SYNC_REFUSED"

    def test_approving_is_not_an_operation(self, tenant, tech):
        from sync.services import SyncRefused

        for operation in ("EXPENSE_DECIDE", "MARK_PAID", "CLOSE_FLOAT"):
            with pytest.raises(SyncRefused):
                send(tenant, tech, operation, {"id": 1, "approved": True})
        assert SyncSubmission.objects.count() == 0


class TestBatchOrder:
    def test_a_casual_and_the_expense_naming_it_both_land(
        self, tenant, tech, finance_user, site, labour
    ):
        casual_uuid = uuid4()
        send(
            tenant,
            tech,
            "CASUAL",
            {"name": "John Kamau", "id_number": "12345678"},
            casual_uuid,
        )
        submission, _ = send(
            tenant,
            tech,
            "EXPENSE",
            expense_body(
                labour,
                site,
                casual_lines=[{"casual_client_uuid": str(casual_uuid), "days": 2}],
            ),
        )

        assert submission.status == SubmissionStatus.APPLIED
        expense = ProjectExpense.objects.get()
        assert [line.casual.client_uuid for line in expense.casual_lines.all()] == [casual_uuid]

    def test_an_unknown_casual_client_uuid_is_refused(
        self, tenant, tech, finance_user, site, labour
    ):
        submission, _ = send(
            tenant,
            tech,
            "EXPENSE",
            expense_body(
                labour, site, casual_lines=[{"casual_client_uuid": str(uuid4()), "days": 1}]
            ),
        )

        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "SYNC_REFUSED"
        assert not ProjectExpense.objects.exists()


class TestIsolation:
    def test_another_tenants_ids_are_refused(
        self, tenant, other_organization, tech, finance_user, site
    ):
        from core.tenancy import tenant_context

        with tenant_context(other_organization):
            foreign = ExpenseCategory.objects.create(
                organization=other_organization, name="Theirs"
            )

        submission, _ = send(
            tenant,
            tech,
            "EXPENSE",
            {
                "category": foreign.pk,
                "site": site.pk,
                "amount": "100",
                "incurred_on": DAY,
            },
        )

        assert submission.status == SubmissionStatus.REJECTED
        assert not ProjectExpense.objects.exists()

    def test_resolving_is_still_possible_on_the_refusal(self, tenant, tech, finance_user, site):
        send(tenant, tech, "ALLOWANCE_REQUEST", allowance_body(site))
        send(tenant, tech, "ALLOWANCE_REQUEST", allowance_body(site))
        exception = SyncException.objects.get()
        resolve_exception(exception, resolution="Handled by phone", resolved_by=tech)
        assert exception.status == ExceptionStatus.RESOLVED


class TestBundle:
    @pytest.fixture
    def http(self, tenant, tech, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        client = APIClient(HTTP_HOST="silvertech.localhost")
        client.force_authenticate(tech)
        return client

    def test_it_carries_the_money_reference_data(
        self, http, tenant, tech, pm, finance_user, site, project, category
    ):
        ExpenseCategory.objects.create(organization=tenant, name="Old", is_active=False)
        casual = finance.register_casual(
            actor=tech, name="John Kamau", id_number="12345678", phone="0700"
        )
        float_request = finance.request_allowance(
            actor=tech,
            type="FLOAT",
            amount=Decimal("5000"),
            from_date=date(2026, 10, 5),
            to_date=date(2026, 10, 5),
            site=site,
        )
        approve_through(float_request, pm=pm, finance_user=finance_user)
        finance.mark_paid(float_request, actor=finance_user, reference="MPESA-1")
        closed_project = ProjectFactory(reference="WO-7002", manager=pm)
        closed_project.sites.add(site)
        type(closed_project).objects.filter(pk=closed_project.pk).update(
            status="CLOSED", closed_at=timezone.now()
        )

        body = http.get("/api/v1/sync/bundle").json()

        assert [row["name"] for row in body["expense_categories"]] == ["Misc"]
        assert set(body["expense_categories"][0]) == {"id", "name", "kind"}
        [row] = body["casuals"]
        assert row["id"] == casual.pk
        assert row["id_number"] == "*****678"
        assert "12345678" not in str(body["casuals"])
        [mine] = body["my_floats"]
        assert mine["id"] == float_request.pk
        assert Decimal(mine["balance"]) == Decimal("5000")
        assert body["finance_limits"] == tenant.settings.allowance_limits
        [listed] = [s for s in body["sites"] if s["id"] == site.pk]
        assert [p["id"] for p in listed["open_projects"]] == [project.pk]

    def test_my_floats_are_only_mine(self, http, tenant, pm, finance_user, site):
        other = UserFactory(organization=tenant)
        float_request = finance.request_allowance(
            actor=other,
            type="FLOAT",
            amount=Decimal("5000"),
            from_date=date(2026, 10, 5),
            to_date=date(2026, 10, 5),
            site=site,
        )
        approve_through(float_request, pm=pm, finance_user=finance_user)
        finance.mark_paid(float_request, actor=finance_user, reference="MPESA-2")

        assert http.get("/api/v1/sync/bundle").json()["my_floats"] == []
