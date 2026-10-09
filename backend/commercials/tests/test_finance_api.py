"""T15.4 — the money-out endpoints (§4.17.6, §4.17.7; R1-R5).

Services are tested in ``test_finance_services``; what is pinned here is what a
client sees: the payload fields the frontend reads, who may call what, the
filters the screens send, and that a page of entries does not cost a query per
row.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

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
from commercials.tests.api_helpers import PASSWORD, Api, error_code, results
from commercials.tests.finance_helpers import approve_through
from network.factories import ProjectFactory, SiteFactory

D = Decimal
DAY = "2026-10-05"


@pytest.fixture(autouse=True)
def _domain(settings):
    settings.TENANT_BASE_DOMAIN = "localhost"


def person(tenant, name, *codenames):
    user = UserFactory(organization=tenant, full_name=name, password=PASSWORD)
    if codenames:
        UserRoleFactory(user=user, role=RoleFactory(codenames=list(codenames)))
    return user


@pytest.fixture
def pm(tenant):
    return person(tenant, "Pippa Manager")


@pytest.fixture
def tech(tenant):
    return person(tenant, "Tom Technician")


@pytest.fixture
def fin(tenant):
    return person(tenant, "Fiona Finance", PERM.FINANCE_APPROVE)


@pytest.fixture
def project(tenant, pm):
    return ProjectFactory(
        reference="WO-9901",
        po_number="PO-990",
        manager=pm,
        contract_value=D("500000.00"),
        cost_budget=D("400000.00"),
    )


@pytest.fixture
def site(tenant, project):
    site = SiteFactory(name="Ruiru")
    project.sites.add(
        site,
        through_defaults={"organization_id": project.organization_id},
    )
    return site


@pytest.fixture
def category(tenant):
    return ExpenseCategory.objects.create(organization=tenant, name="Misc")


@pytest.fixture
def fuel(tenant):
    return ExpenseCategory.objects.create(organization=tenant, name="Fuel", kind=ExpenseKind.FUEL)


@pytest.fixture
def labour(tenant):
    return ExpenseCategory.objects.create(
        organization=tenant, name="Casual labour", kind=ExpenseKind.CASUAL_LABOUR
    )


def api(client, user):
    return Api(client, user)


def expense_body(category, site, **extra):
    return {
        "category": category.pk,
        "site": site.pk,
        "amount": "1000.00",
        "incurred_on": DAY,
        "description": "Receipt",
        **extra,
    }


def spend(tech, category, site, **extra):
    return finance.record_expense(
        actor=tech,
        category=category,
        amount=D("1000.00"),
        incurred_on=date(2026, 10, 5),
        site=site,
        **extra,
    )


def ask(actor, site, type="NIGHT_OUT", amount="2000", **extra):
    return finance.request_allowance(
        actor=actor,
        type=type,
        amount=D(amount),
        from_date=date(2026, 10, 5),
        to_date=date(2026, 10, 5),
        site=site,
        **extra,
    )


def allowance_body(site, **extra):
    return {
        "type": "NIGHT_OUT",
        "amount": "2000",
        "from_date": DAY,
        "to_date": DAY,
        "site": site.pk,
        "reason": "Night shift",
        **extra,
    }


# --------------------------------------------------------------------------
# Expenses
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecordingAnExpense:
    def test_the_payload_carries_what_the_screens_read(
        self, client, tech, fin, site, category, project
    ):
        response = api(client, tech).post(
            "project-expenses", expense_body(category, site, scope_of_work="Mast")
        )

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["status"] == "PENDING_PM"
        assert body["site"] == site.pk
        assert body["site_name"] == "Ruiru"
        assert body["project"] == project.pk
        assert body["project_reference"]
        assert body["category_name"] == "Misc"
        assert body["category_kind"] == "GENERAL"
        assert body["recorded_by"] == tech.pk
        assert body["recorded_by_name"] == "Tom Technician"
        assert body["scope_of_work"] == "Mast"
        assert body["evidence_state"] == "none"
        assert body["pm_level_skipped"] is False
        assert body["casual_lines"] == []
        assert body["paid_at"] is None and body["payment_reference"] == ""
        assert body["is_reversal"] is False

    def test_a_pm_recording_their_own_goes_straight_to_finance(
        self, client, pm, fin, site, category
    ):
        """R4: the explicit flag the Approvals screen switches on."""
        body = api(client, pm).post("project-expenses", expense_body(category, site)).json()

        assert body["status"] == "PENDING_FINANCE"
        assert body["pm_level_skipped"] is True

    def test_a_replayed_client_uuid_returns_the_same_expense(
        self, client, tech, fin, site, category
    ):
        key = str(uuid.uuid4())
        first = api(client, tech).post(
            "project-expenses", expense_body(category, site, client_uuid=key)
        )
        again = api(client, tech).post(
            "project-expenses", expense_body(category, site, client_uuid=key)
        )

        assert first.json()["id"] == again.json()["id"]
        assert ProjectExpense.objects.count() == 1

    def test_the_evidence_state_tells_arriving_from_none(self, client, tech, fin, site, category):
        arriving = api(client, tech).post(
            "project-expenses", expense_body(category, site, photos_expected=2)
        )

        assert arriving.json()["evidence_state"] == "arriving"

    def test_fuel_asks_for_the_vehicle(self, client, tech, fin, site, fuel):
        response = api(client, tech).post("project-expenses", expense_body(fuel, site))

        assert response.status_code == 400
        assert error_code(response) == "FINANCE_INPUT_INVALID"
        assert "vehicle_reg" in response.json()["error"]["field_errors"]

    def test_casual_labour_carries_its_lines_with_names(self, client, tech, fin, site, labour):
        casual = Casual.objects.create(name="Juma Otieno", id_number="12345678", registered_by=tech)

        response = api(client, tech).post(
            "project-expenses",
            expense_body(
                labour, site, casual_lines=[{"casual": casual.pk, "days": 2, "amount": "600"}]
            ),
        )

        assert response.status_code == 201, response.content
        line = response.json()["casual_lines"][0]
        assert line["casual"] == casual.pk
        assert line["casual_name"] == "Juma Otieno"
        assert line["days"] == 2
        assert line["amount"] == "600.00"

    def test_two_open_projects_on_a_site_need_a_choice(
        self, client, tech, fin, site, category, project
    ):
        other = ProjectFactory(
            reference="WO-9902",
            po_number="PO-991",
            manager=project.manager,
            contract_value=D("1.00"),
            cost_budget=D("1.00"),
        )
        other.sites.add(
            site,
            through_defaults={"organization_id": other.organization_id},
        )
        response = api(client, tech).post("project-expenses", expense_body(category, site))

        assert response.status_code == 400
        assert error_code(response) == "PROJECT_AMBIGUOUS"

    def test_nobody_but_the_recorder_to_approve_is_refused(self, client, tech, site, category):
        """No finance.approve holder exists in this tenant."""
        response = api(client, tech).post("project-expenses", expense_body(category, site))

        assert response.status_code == 409
        assert error_code(response) == "FINANCE_NO_OTHER_APPROVER"


@pytest.mark.django_db
class TestListingExpenses:
    @pytest.fixture
    def entries(self, tenant, tech, pm, fin, site, category, project):
        mine = spend(tech, category, site)
        theirs = spend(pm, category, site)
        return mine, theirs

    def test_mine_is_only_my_own(self, client, tech, entries):
        rows = results(api(client, tech).get("project-expenses", mine="true"))

        assert [row["id"] for row in rows] == [entries[0].pk]

    def test_a_member_sees_only_their_own(self, client, tech, entries):
        """Colleagues' money is not for browsing (R4)."""
        rows = results(api(client, tech).get("project-expenses"))

        assert [row["id"] for row in rows] == [entries[0].pk]

    def test_finance_and_the_projects_pm_see_everyone(self, client, pm, fin, entries):
        assert len(results(api(client, fin).get("project-expenses"))) == 2
        assert len(results(api(client, pm).get("project-expenses"))) == 2

    def test_filters_by_status_project_and_float(self, client, tech, fin, entries, project):
        http = api(client, fin)

        assert len(results(http.get("project-expenses", status="PENDING_FINANCE"))) == 1
        assert len(results(http.get("project-expenses", project=project.pk))) == 2
        other_float = ask(tech, entries[0].site, type="FLOAT", amount="100")
        assert results(http.get("project-expenses", float_request=other_float.pk)) == []

    def test_a_float_ledger_is_its_expenses(self, client, tenant, tech, pm, fin, site, category):
        float_request = ask(tech, site, type="FLOAT", amount="5000")
        approve_through(float_request, pm=pm, finance_user=fin)
        finance.mark_paid(float_request, actor=fin, reference="MPESA1")
        on_float = spend(tech, category, site, float_request=float_request)
        spend(tech, category, site)

        rows = results(api(client, tech).get("project-expenses", float_request=float_request.pk))

        assert [row["id"] for row in rows] == [on_float.pk]

    def test_payable_is_finance_only(self, client, tech, entries):
        assert api(client, tech).get("project-expenses", payable="true").status_code == 403

    def test_payable_is_approved_unpaid_and_not_from_a_float(
        self, client, tenant, tech, pm, fin, site, category
    ):
        owed = approve_through(spend(tech, category, site), pm=pm, finance_user=fin)
        paid = approve_through(spend(tech, category, site), pm=pm, finance_user=fin)
        finance.mark_paid(paid, actor=fin, reference="MPESA1")
        float_request = ask(tech, site, type="FLOAT", amount="5000")
        approve_through(float_request, pm=pm, finance_user=fin)
        finance.mark_paid(float_request, actor=fin, reference="MPESA2")
        approve_through(
            spend(tech, category, site, float_request=float_request), pm=pm, finance_user=fin
        )
        spend(tech, category, site)  # still pending

        rows = results(api(client, fin).get("project-expenses", payable="true"))

        assert [row["id"] for row in rows] == [owed.pk]

    def test_a_page_does_not_cost_a_query_per_row(self, client, tenant, tech, fin, site, category):
        http = api(client, tech)
        spend(tech, category, site)
        with CaptureQueriesContext(connection) as small:
            http.get("project-expenses")
        for _ in range(6):
            spend(tech, category, site)
        with CaptureQueriesContext(connection) as large:
            response = http.get("project-expenses")

        assert len(results(response)) == 7
        assert len(large) == len(small)


@pytest.mark.django_db
class TestPendingIsLevelAware:
    def test_the_pm_sees_theirs_then_finance_sees_it_after(
        self, client, tenant, tech, pm, fin, site, category
    ):
        expense = spend(tech, category, site)
        as_pm, as_fin = api(client, pm), api(client, fin)

        assert [r["id"] for r in results(as_pm.get("project-expenses/pending"))] == [expense.pk]
        assert results(as_fin.get("project-expenses/pending")) == []

        decided = as_pm.post(f"project-expenses/{expense.pk}/decide", {"approved": True})
        assert decided.status_code == 200, decided.content
        assert decided.json()["status"] == "PENDING_FINANCE"

        assert results(as_pm.get("project-expenses/pending")) == []
        assert [r["id"] for r in results(as_fin.get("project-expenses/pending"))] == [expense.pk]

    def test_a_technician_sees_nothing_to_decide(
        self, client, tenant, tech, pm, fin, site, category
    ):
        spend(tech, category, site)

        assert results(api(client, tech).get("project-expenses/pending")) == []

    def test_another_pm_does_not_see_this_projects_entries(
        self, client, tenant, tech, fin, site, category
    ):
        spend(tech, category, site)
        stranger = person(tenant, "Other Manager")

        assert results(api(client, stranger).get("project-expenses/pending")) == []


@pytest.mark.django_db
class TestDecidingPayingReversing:
    def test_the_whole_journey(self, client, tenant, tech, pm, fin, site, category):
        expense = spend(tech, category, site)
        base = f"project-expenses/{expense.pk}"

        api(client, pm).post(f"{base}/decide", {"approved": True})
        done = api(client, fin).post(f"{base}/decide", {"approved": True})
        assert done.json()["status"] == "APPROVED"

        paid = api(client, fin).post(f"{base}/mark-paid", {"payment_reference": "MPESA-77"})
        assert paid.status_code == 200, paid.content
        assert paid.json()["status"] == "PAID"
        assert paid.json()["payment_reference"] == "MPESA-77"
        assert paid.json()["paid_by"] == fin.pk
        assert paid.json()["paid_at"]

    def test_the_recorder_cannot_decide_their_own(
        self, client, tenant, tech, pm, fin, site, category
    ):
        expense = spend(tech, category, site)

        response = api(client, tech).post(
            f"project-expenses/{expense.pk}/decide", {"approved": True}
        )

        assert response.status_code == 403
        assert error_code(response) == "FINANCE_SELF_APPROVAL"

    def test_finance_cannot_answer_the_pm_level(
        self, client, tenant, tech, pm, fin, site, category
    ):
        expense = spend(tech, category, site)

        response = api(client, fin).post(
            f"project-expenses/{expense.pk}/decide", {"approved": True}
        )

        assert response.status_code == 403

    def test_rejecting_needs_a_reason(self, client, tenant, tech, pm, fin, site, category):
        expense = spend(tech, category, site)

        response = api(client, pm).post(
            f"project-expenses/{expense.pk}/decide", {"approved": False}
        )

        assert response.status_code == 400
        assert error_code(response) == "REJECTION_REASON_REQUIRED"

    def test_reject_then_resubmit_by_the_recorder_only(
        self, client, tenant, tech, pm, fin, site, category
    ):
        expense = spend(tech, category, site)
        base = f"project-expenses/{expense.pk}"
        rejected = api(client, pm).post(
            f"{base}/decide", {"approved": False, "reason": "Blurred receipt"}
        )
        assert rejected.json()["status"] == "REJECTED"
        assert rejected.json()["decision_reason"] == "Blurred receipt"

        assert api(client, pm).post(f"{base}/resubmit").status_code == 403
        again = api(client, tech).post(f"{base}/resubmit")

        assert again.status_code == 200, again.content
        assert again.json()["status"] == "PENDING_PM"

    def test_marking_paid_is_finance_only(self, client, tenant, tech, pm, fin, site, category):
        expense = approve_through(spend(tech, category, site), pm=pm, finance_user=fin)

        response = api(client, pm).post(
            f"project-expenses/{expense.pk}/mark-paid", {"payment_reference": "X"}
        )

        assert response.status_code == 403

    def test_paying_needs_a_reference(self, client, tenant, tech, pm, fin, site, category):
        expense = approve_through(spend(tech, category, site), pm=pm, finance_user=fin)

        response = api(client, fin).post(f"project-expenses/{expense.pk}/mark-paid", {})

        assert response.status_code == 400
        assert error_code(response) == "PAYMENT_REFERENCE_REQUIRED"

    def test_a_float_backed_expense_is_not_paid_again(
        self, client, tenant, tech, pm, fin, site, category
    ):
        float_request = ask(tech, site, type="FLOAT", amount="5000")
        approve_through(float_request, pm=pm, finance_user=fin)
        finance.mark_paid(float_request, actor=fin, reference="MPESA1")
        expense = approve_through(
            spend(tech, category, site, float_request=float_request), pm=pm, finance_user=fin
        )

        response = api(client, fin).post(
            f"project-expenses/{expense.pk}/mark-paid", {"payment_reference": "Y"}
        )

        assert response.status_code == 409
        assert error_code(response) == "FLOAT_BACKED_NOT_PAYABLE"

    def test_reversal_is_for_the_pm_or_finance(self, client, tenant, tech, pm, fin, site, category):
        expense = approve_through(spend(tech, category, site), pm=pm, finance_user=fin)
        base = f"project-expenses/{expense.pk}/reverse"

        assert api(client, tech).post(base, {"reason": "Mistake"}).status_code == 403
        reversed_ = api(client, pm).post(base, {"reason": "Mistake"})

        assert reversed_.status_code == 201, reversed_.content
        assert reversed_.json()["is_reversal"] is True
        assert reversed_.json()["reverses"] == expense.pk


@pytest.mark.django_db
class TestEditingAPendingExpense:
    def test_the_recorder_may_fix_the_words_while_the_pm_has_it(
        self, client, tenant, tech, pm, fin, site, category
    ):
        expense = spend(tech, category, site)

        response = api(client, tech).patch(
            f"project-expenses/{expense.pk}",
            {"description": "Diesel", "amount": "1.00"},
        )

        assert response.status_code == 200, response.content
        assert response.json()["description"] == "Diesel"
        # Money is not editable: that would change what the PM is approving.
        assert response.json()["amount"] == "1000.00"

    def test_somebody_else_may_not(self, client, tenant, tech, pm, fin, site, category):
        expense = spend(tech, category, site)

        response = api(client, pm).patch(f"project-expenses/{expense.pk}", {"description": "x"})

        assert response.status_code == 403

    def test_not_once_the_pm_has_answered(self, client, tenant, tech, pm, fin, site, category):
        expense = spend(tech, category, site)
        finance.decide(expense, actor=pm, approved=True)

        response = api(client, tech).patch(f"project-expenses/{expense.pk}", {"description": "x"})

        assert response.status_code == 409

    def test_there_is_no_delete(self, client, tenant, tech, fin, site, category):
        expense = spend(tech, category, site)

        assert api(client, tech).delete(f"project-expenses/{expense.pk}").status_code == 405


# --------------------------------------------------------------------------
# Allowance requests
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestRequestingAnAllowance:
    def test_the_payload_carries_what_the_screens_read(self, client, tech, fin, site, project):
        response = api(client, tech).post(
            "allowance-requests",
            allowance_body(site, to_date="2026-10-06", amount="4000"),
        )

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["number"].startswith("AR")
        assert body["status"] == "PENDING_PM"
        assert body["days"] == 2
        assert body["daily_amount"] == "2000.00"
        assert body["site_name"] == "Ruiru"
        assert body["project_reference"]
        assert body["recorded_by_name"] == "Tom Technician"
        assert body["transport_scope"] is None
        assert body["returned_amount"] is None
        assert body["closed_at"] is None
        assert body["open_float_warning"] is None
        assert body["pm_level_skipped"] is False
        # Only a float has a ledger.
        assert "spent" not in body and "balance" not in body

    def test_transport_needs_a_scope(self, client, tech, fin, site):
        response = api(client, tech).post(
            "allowance-requests", allowance_body(site, type="TRANSPORT", amount="300")
        )

        assert response.status_code == 400
        assert error_code(response) == "TRANSPORT_SCOPE_REQUIRED"

        ok = api(client, tech).post(
            "allowance-requests",
            allowance_body(site, type="TRANSPORT", amount="300", transport_scope="WITHIN_NAIROBI"),
        )
        assert ok.status_code == 201, ok.content
        assert ok.json()["transport_scope"] == "WITHIN_NAIROBI"

    def test_overlap_is_refused_naming_the_earlier_one(self, client, tech, fin, site):
        first = api(client, tech).post("allowance-requests", allowance_body(site)).json()

        response = api(client, tech).post("allowance-requests", allowance_body(site))

        assert response.status_code == 409
        assert error_code(response) == "ALLOWANCE_OVERLAP"
        assert first["number"] in response.json()["error"]["message"]

    def test_a_limit_breach_is_refused(self, client, tech, fin, site):
        response = api(client, tech).post("allowance-requests", allowance_body(site, amount="50"))

        assert response.status_code == 400
        assert error_code(response) == "ALLOWANCE_LIMIT"

    def test_a_replayed_client_uuid_returns_the_same_request(self, client, tech, fin, site):
        key = str(uuid.uuid4())
        first = api(client, tech).post("allowance-requests", allowance_body(site, client_uuid=key))
        again = api(client, tech).post("allowance-requests", allowance_body(site, client_uuid=key))

        assert first.json()["id"] == again.json()["id"]
        assert AllowanceRequest.objects.count() == 1

    def test_there_is_no_patch(self, client, tenant, tech, fin, site):
        request = ask(tech, site)

        assert (
            api(client, tech).patch(f"allowance-requests/{request.pk}", {"amount": "9"}).status_code
            == 405
        )


@pytest.mark.django_db
class TestAllowanceFiltersAndQueues:
    @pytest.fixture
    def requests(self, tenant, tech, pm, fin, site):
        night = ask(tech, site, type="NIGHT_OUT")
        team = ask(pm, site, type="TEAM_ALLOWANCE")
        return night, team

    def test_mine_type_and_status(self, client, tech, fin, requests):
        assert [
            r["id"] for r in results(api(client, tech).get("allowance-requests", mine="true"))
        ] == [requests[0].pk]
        # Another person's request is Finance's to see, not a colleague's (R4).
        assert [r["id"] for r in results(api(client, tech).get("allowance-requests"))] == [
            requests[0].pk
        ]
        http = api(client, fin)
        assert [
            r["id"] for r in results(http.get("allowance-requests", type="TEAM_ALLOWANCE"))
        ] == [requests[1].pk]
        assert [
            r["id"] for r in results(http.get("allowance-requests", status="PENDING_FINANCE"))
        ] == [requests[1].pk]

    def test_payable_is_finance_only_and_approved_unpaid(
        self, client, tenant, tech, pm, fin, site, requests
    ):
        assert api(client, tech).get("allowance-requests", payable="true").status_code == 403
        approve_through(requests[0], pm=pm, finance_user=fin)

        rows = results(api(client, fin).get("allowance-requests", payable="true"))

        assert [row["id"] for row in rows] == [requests[0].pk]

    def test_pending_is_level_aware(self, client, tenant, tech, pm, fin, site, requests):
        # tech's goes to the PM; pm's own skipped to Finance.
        assert [r["id"] for r in results(api(client, pm).get("allowance-requests/pending"))] == [
            requests[0].pk
        ]
        assert [r["id"] for r in results(api(client, fin).get("allowance-requests/pending"))] == [
            requests[1].pk
        ]
        assert results(api(client, tech).get("allowance-requests/pending")) == []

    def test_the_journey_and_pm_skip_flag(self, client, tenant, tech, pm, fin, site, requests):
        night, team = requests
        flags = {
            row["id"]: row["pm_level_skipped"]
            for row in results(api(client, fin).get("allowance-requests"))
        }
        assert flags == {night.pk: False, team.pk: True}

        done = api(client, fin).post(f"allowance-requests/{team.pk}/decide", {"approved": True})
        assert done.json()["status"] == "APPROVED"
        paid = api(client, fin).post(
            f"allowance-requests/{team.pk}/mark-paid", {"payment_reference": "MPESA9"}
        )
        assert paid.json()["status"] == "PAID"

    def test_marking_paid_is_finance_only(self, client, tenant, tech, pm, fin, site, requests):
        approve_through(requests[0], pm=pm, finance_user=fin)

        response = api(client, pm).post(
            f"allowance-requests/{requests[0].pk}/mark-paid", {"payment_reference": "X"}
        )

        assert response.status_code == 403

    def test_a_rejected_request_is_resubmitted_by_its_recorder(
        self, client, tenant, tech, pm, fin, site, requests
    ):
        night = requests[0]
        api(client, pm).post(
            f"allowance-requests/{night.pk}/decide", {"approved": False, "reason": "Dates"}
        )

        again = api(client, tech).post(f"allowance-requests/{night.pk}/resubmit")

        assert again.status_code == 200, again.content
        assert again.json()["status"] == "PENDING_PM"


@pytest.mark.django_db
class TestFloats:
    @pytest.fixture
    def open_float(self, tenant, tech, pm, fin, site):
        float_request = ask(tech, site, type="FLOAT", amount="5000")
        approve_through(float_request, pm=pm, finance_user=fin)
        finance.mark_paid(float_request, actor=fin, reference="MPESA1")
        return float_request

    def test_spent_and_balance(self, client, tenant, tech, open_float, site, category):
        spend(tech, category, site, float_request=open_float)

        body = api(client, tech).get(f"allowance-requests/{open_float.pk}").json()

        assert body["spent"] == "1000.00"
        assert body["balance"] == "4000.00"
        assert body["returned_amount"] is None

    def test_a_rejected_expense_is_not_spent(
        self, client, tenant, tech, pm, open_float, site, category
    ):
        expense = spend(tech, category, site, float_request=open_float)
        finance.decide(expense, actor=pm, approved=False, reason="No receipt")

        body = api(client, tech).get(f"allowance-requests/{open_float.pk}").json()

        assert body["spent"] == "0.00"
        assert body["balance"] == "5000.00"

    def test_closing_records_what_came_back(
        self, client, tenant, tech, fin, open_float, site, category
    ):
        spend(tech, category, site, float_request=open_float)

        response = api(client, fin).post(
            f"allowance-requests/{open_float.pk}/close-float", {"returned_amount": "500"}
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["closed_at"]
        assert body["closed_by"] == fin.pk
        assert body["returned_amount"] == "500.00"
        assert body["balance"] == "3500.00"

    def test_closing_is_finance_only_and_only_once(self, client, tenant, tech, fin, open_float):
        url = f"allowance-requests/{open_float.pk}/close-float"

        assert api(client, tech).post(url, {"returned_amount": "0"}).status_code == 403
        assert api(client, fin).post(url, {"returned_amount": "0"}).status_code == 200
        again = api(client, fin).post(url, {"returned_amount": "0"})
        assert again.status_code == 409
        assert error_code(again) == "FLOAT_NOT_OPEN"

    def test_a_second_float_request_carries_the_warning(
        self, client, tenant, tech, pm, fin, site, open_float
    ):
        second = ask(tech, site, type="FLOAT", amount="2000")

        body = api(client, pm).get(f"allowance-requests/{second.pk}").json()

        assert body["open_float_warning"] == {
            "number": open_float.number,
            "balance": "5000.00",
        }
        # Never on the one that is itself the open float.
        assert (
            api(client, pm).get(f"allowance-requests/{open_float.pk}").json()["open_float_warning"]
            is None
        )

    def test_pending_floats_do_not_cost_a_query_per_row(
        self, client, tenant, tech, pm, fin, site, open_float
    ):
        # Three recorders, each with a second float waiting on the PM.
        extra = [person(tenant, f"Tech {n}") for n in range(3)]
        for user in extra:
            ask(user, site, type="FLOAT", amount="100")
        http = api(client, pm)
        with CaptureQueriesContext(connection) as many:
            response = http.get("allowance-requests/pending")
        assert len(results(response)) == 3
        ask(person(tenant, "Tech X"), site, type="FLOAT", amount="100")
        with CaptureQueriesContext(connection) as more:
            http.get("allowance-requests/pending")

        # One more recorder, one more cached lookup at most.
        assert len(more) <= len(many) + 1


# --------------------------------------------------------------------------
# Casuals
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestCasuals:
    def test_any_member_registers_one(self, client, tech, fin):
        response = api(client, tech).post(
            "casuals", {"name": "Juma Otieno", "id_number": "12 345-678", "phone": "0722000111"}
        )

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["name"] == "Juma Otieno"
        assert body["registered_by"] == tech.pk
        assert Casual.objects.get().id_number_key == "12345678"

    def test_the_same_id_twice_names_the_existing_casual(self, client, tech):
        api(client, tech).post("casuals", {"name": "Juma", "id_number": "12345678"})

        response = api(client, tech).post("casuals", {"name": "Jay", "id_number": "12-345 678"})

        assert response.status_code == 409
        assert error_code(response) == "CASUAL_ID_DUPLICATE"

    def test_a_replayed_client_uuid_returns_the_same_casual(self, client, tech):
        key = str(uuid.uuid4())
        first = api(client, tech).post(
            "casuals", {"name": "Juma", "id_number": "12345678", "client_uuid": key}
        )
        again = api(client, tech).post(
            "casuals", {"name": "Juma", "id_number": "12345678", "client_uuid": key}
        )

        assert first.json()["id"] == again.json()["id"]

    def test_the_id_number_is_masked_except_for_finance(self, client, tech, fin):
        made = api(client, tech).post("casuals", {"name": "Juma", "id_number": "AB12345678"})
        pk = made.json()["id"]

        assert made.json()["id_number"] == "*******678"
        assert api(client, tech).get(f"casuals/{pk}").json()["id_number"] == "*******678"
        assert results(api(client, tech).get("casuals"))[0]["id_number"] == "*******678"
        assert api(client, fin).get(f"casuals/{pk}").json()["id_number"] == "AB12345678"
        assert results(api(client, fin).get("casuals"))[0]["id_number"] == "AB12345678"

    def test_search_by_name_phone_or_id(self, client, tech, fin):
        http = api(client, tech)
        http.post(
            "casuals", {"name": "Juma Otieno", "id_number": "11111111", "phone": "0722000111"}
        )
        http.post(
            "casuals", {"name": "Wanjiru Kamau", "id_number": "22222222", "phone": "0733000222"}
        )

        def found(term):
            return [row["name"] for row in results(http.get("casuals", search=term))]

        assert found("juma") == ["Juma Otieno"]
        assert found("0733") == ["Wanjiru Kamau"]
        # A match would confirm digits the mask hides, so only Finance searches by ID.
        assert found("2222") == []
        assert [
            row["name"] for row in results(api(client, fin).get("casuals", search="2222"))
        ] == ["Wanjiru Kamau"]
        assert len(results(http.get("casuals"))) == 2

    def test_only_name_and_phone_can_change(self, client, tech):
        pk = (
            api(client, tech)
            .post("casuals", {"name": "Juma", "id_number": "12345678"})
            .json()["id"]
        )

        response = api(client, tech).patch(
            f"casuals/{pk}",
            {"name": "Juma O.", "phone": "0700", "id_number": "99999999"},
        )

        assert response.status_code == 200, response.content
        casual = Casual.objects.get(pk=pk)
        assert (casual.name, casual.phone) == ("Juma O.", "0700")
        assert casual.id_number == "12345678"


# --------------------------------------------------------------------------
# Settings and categories
# --------------------------------------------------------------------------

LIMITS = {
    "TRANSPORT_WITHIN_NAIROBI": {"min": None, "max": "600"},
    "TRANSPORT_OUTSIDE_NAIROBI": {"min": None, "max": None},
    "NIGHT_OUT": {"min": "1000", "max": "8000"},
    "TEAM_ALLOWANCE": {"min": "1500", "max": "10000"},
}


@pytest.mark.django_db
class TestFinanceSettings:
    def test_any_member_reads_the_defaults(self, client, tech):
        body = api(client, tech).get("finance/settings").json()

        assert set(body["allowance_limits"]) == set(LIMITS)
        assert body["allowance_limits"]["NIGHT_OUT"] == {"min": "1500", "max": "10000"}
        assert body["finance_director_role"] is None

    def test_a_technician_cannot_write(self, client, tech):
        response = api(client, tech).patch("finance/settings", {"allowance_limits": LIMITS})

        assert response.status_code == 403

    def test_finance_sets_limits_and_the_director_role(self, client, tenant, fin):
        role = RoleFactory(name="Director")

        response = api(client, fin).patch(
            "finance/settings",
            {"allowance_limits": LIMITS, "finance_director_role": role.pk},
        )

        assert response.status_code == 200, response.content
        assert response.json()["allowance_limits"]["TRANSPORT_WITHIN_NAIROBI"]["max"] == "600"
        assert response.json()["finance_director_role"] == role.pk
        settings_object = fin.organization.settings
        settings_object.refresh_from_db()
        assert settings_object.finance_director_role_id == role.pk

        cleared = api(client, fin).patch("finance/settings", {"finance_director_role": None})
        assert cleared.json()["finance_director_role"] is None
        # Limits survive a patch that does not mention them.
        assert cleared.json()["allowance_limits"]["NIGHT_OUT"]["min"] == "1000"

    def test_settings_manage_may_write_too(self, client, tenant):
        manager = person(tenant, "Ops", PERM.SETTINGS_MANAGE)

        response = api(client, manager).patch("finance/settings", {"allowance_limits": LIMITS})

        assert response.status_code == 200, response.content

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda limits: limits.pop("NIGHT_OUT"),
            lambda limits: limits.update(BOGUS={"min": None, "max": None}),
            lambda limits: limits.update(NIGHT_OUT={"min": "9000", "max": "1000"}),
            lambda limits: limits.update(NIGHT_OUT={"min": "abc", "max": None}),
            lambda limits: limits.update(NIGHT_OUT={"min": "-1", "max": None}),
            lambda limits: limits.update(NIGHT_OUT={"min": None}),
            lambda limits: limits.update(NIGHT_OUT="1000"),
            lambda limits: limits.update(NIGHT_OUT={"min": True, "max": None}),
        ],
        ids=[
            "missing key",
            "unknown key",
            "min above max",
            "not a number",
            "negative",
            "no max",
            "not an object",
            "a boolean",
        ],
    )
    def test_the_shape_is_validated(self, client, fin, mutate):
        limits = {key: dict(value) for key, value in LIMITS.items()}
        mutate(limits)

        response = api(client, fin).patch("finance/settings", {"allowance_limits": limits})

        assert response.status_code == 400, response.content
        assert "allowance_limits" in response.json()["error"]["field_errors"]

    def test_a_role_must_be_a_real_one(self, client, fin):
        response = api(client, fin).patch("finance/settings", {"finance_director_role": 987654})

        assert response.status_code == 400
        assert "finance_director_role" in response.json()["error"]["field_errors"]

    def test_a_new_limit_applies_to_the_next_request(self, client, tenant, tech, fin, site):
        api(client, fin).patch(
            "finance/settings",
            {"allowance_limits": {**LIMITS, "NIGHT_OUT": {"min": None, "max": "1000"}}},
        )

        response = api(client, tech).post("allowance-requests", allowance_body(site, amount="2000"))

        assert error_code(response) == "ALLOWANCE_LIMIT"


@pytest.mark.django_db
class TestExpenseCategories:
    def test_kind_is_read_and_written(self, client, fin):
        made = api(client, fin).post(
            "expense-categories", {"name": "Diesel", "code": "DSL", "kind": "FUEL"}
        )

        assert made.status_code == 201, made.content
        assert made.json()["kind"] == "FUEL"
        changed = api(client, fin).patch(
            f"expense-categories/{made.json()['id']}", {"kind": "GENERAL"}
        )
        assert changed.json()["kind"] == "GENERAL"

    def test_catalogue_manage_may_write_too(self, client, tenant):
        manager = person(tenant, "Cat", PERM.CATALOGUE_MANAGE)

        response = api(client, manager).post("expense-categories", {"name": "Hire"})

        assert response.status_code == 201

    def test_a_technician_may_read_but_not_write(self, client, tenant, tech, category):
        http = api(client, tech)

        assert http.get("expense-categories").status_code == 200
        assert http.post("expense-categories", {"name": "Nope"}).status_code == 403
        assert http.patch(f"expense-categories/{category.pk}", {"kind": "FUEL"}).status_code == 403


@pytest.mark.django_db
class TestProjectFilters:
    def test_site_and_status(self, client, tenant, tech, pm, site, project):
        money = {"contract_value": D("1.00"), "cost_budget": D("1.00")}
        other = ProjectFactory(reference="WO-8000", po_number="PO-8000", manager=pm, **money)
        closed = ProjectFactory(
            reference="WO-8001",
            po_number="PO-8001",
            manager=pm,
            status="CLOSED",
            closed_at=timezone.now(),
            **money,
        )
        closed.sites.add(
            site,
            through_defaults={"organization_id": closed.organization_id},
        )
        http = api(client, tech)

        on_site = {row["id"] for row in results(http.get("projects", site=site.pk))}
        open_on_site = {
            row["id"] for row in results(http.get("projects", site=site.pk, status="OPEN"))
        }

        assert on_site == {project.pk, closed.pk}
        assert open_on_site == {project.pk}
        assert other.pk not in on_site
