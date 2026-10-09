"""T18.8, T18.11 (subcontract half) — contracts, jobs under them, payments (§4.19.4; R8).

Services first, then the endpoints a client sees. The worked example at the
bottom is hand computed.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals.engine import ProjectHasNoActiveManager
from approvals.models import ApprovalRequest
from commercials import contracts
from commercials.models import ExpenseStatus, SubcontractPayment
from commercials.reports_finance import SubcontractorSpendReport
from commercials.tests.api_helpers import PASSWORD, Api, error_code, results
from core.exceptions import DomainError, PermissionDeniedError
from core.models import Attachment, AuditAction, AuditLog
from jobs.models import DeliveryMode, Job, JobStatus
from network.factories import ProjectFactory, SiteFactory
from network.models import Subcontractor

D = Decimal
S = ExpenseStatus
TODAY = timezone.localdate()


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
def fin(tenant):
    return person(tenant, "Fiona Finance", PERM.FINANCE_APPROVE)


@pytest.fixture
def fin2(tenant):
    return person(tenant, "Fred Finance", PERM.FINANCE_APPROVE)


@pytest.fixture
def jobber(tenant):
    return person(tenant, "Jo Jobs", PERM.JOB_MANAGE)


@pytest.fixture
def tech(tenant):
    return person(tenant, "Tom Technician")


@pytest.fixture
def site(tenant):
    return SiteFactory(name="Ruiru")


@pytest.fixture
def site2(tenant):
    return SiteFactory(name="Thika")


@pytest.fixture
def stray(tenant):
    return SiteFactory(name="Not on the project")


@pytest.fixture
def project(tenant, pm, site, site2):
    project = ProjectFactory(
        reference="WO-1808",
        po_number="PO-1808",
        manager=pm,
        contract_value=D("900000.00"),
        cost_budget=D("700000.00"),
    )
    for each in (site, site2):
        project.sites.add(each, through_defaults={"organization_id": project.organization_id})
    return project


@pytest.fixture
def acme(tenant):
    return Subcontractor.objects.create(organization=tenant, name="Acme Civils")


@pytest.fixture
def other_sub(tenant):
    return Subcontractor.objects.create(organization=tenant, name="Other Builders")


@pytest.fixture
def contract(tenant, project, acme, fin, site):
    return contracts.create_subcontract(
        actor=fin,
        project=project,
        subcontractor=acme,
        contract_value=D("100000.00"),
        sites=[site],
        payment_terms="30 days",
    )


def po_project(reference, manager):
    return ProjectFactory(
        reference=reference,
        po_number=f"PO-{reference}",
        manager=manager,
        contract_value=D("1000.00"),
        cost_budget=D("500.00"),
    )


def make_job(tenant, project, site, who, subcontractor, price, *, status=JobStatus.OPEN, link=None):
    return Job.objects.create(
        organization=tenant,
        client=site.client,
        site=site,
        project=project,
        assignee=who,
        delivery_mode=DeliveryMode.SUBCONTRACTED,
        subcontractor=subcontractor,
        agreed_price=D(price),
        subcontract=link,
        status=status,
        closed_at=timezone.now() if status == JobStatus.CLOSED else None,
    )


def pay(contract, fin, amount="1000.00", reference="INV-1", **extra):
    return contracts.record_subcontract_payment(
        actor=fin,
        subcontract=contract,
        amount=D(amount),
        paid_on=extra.pop("paid_on", TODAY),
        reference=reference,
        **extra,
    )


def approved_payment(contract, fin, pm, amount="1000.00", reference="INV-1"):
    payment = pay(contract, fin, amount, reference)
    return contracts.decide(payment, actor=pm, approved=True)


# --------------------------------------------------------------------------
# Services: the contract
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateAndEdit:
    def test_finance_creates_with_a_number_and_sites(self, contract, site):
        assert contract.number.startswith("SC")
        assert list(contract.sites.all()) == [site]
        assert contract.status == "ACTIVE"
        assert AuditLog.objects.filter(action=AuditAction.DOCUMENT_POSTED).exists()

    def test_the_project_manager_may_create(self, project, acme, pm):
        made = contracts.create_subcontract(
            actor=pm, project=project, subcontractor=acme, contract_value=D("5.00")
        )
        assert made.created_by == pm

    def test_somebody_else_may_not(self, project, acme, tech):
        with pytest.raises(PermissionDeniedError):
            contracts.create_subcontract(
                actor=tech, project=project, subcontractor=acme, contract_value=D("5.00")
            )

    def test_sites_must_be_on_the_project(self, project, acme, fin, stray):
        with pytest.raises(DomainError) as caught:
            contracts.create_subcontract(
                actor=fin,
                project=project,
                subcontractor=acme,
                contract_value=D("5.00"),
                sites=[stray],
            )
        assert "sites" in caught.value.field_errors

    def test_the_value_must_be_positive(self, project, acme, fin):
        with pytest.raises(DomainError):
            contracts.create_subcontract(
                actor=fin, project=project, subcontractor=acme, contract_value=D("0")
            )

    def test_a_deactivated_subcontractor_is_refused(self, project, acme, fin):
        acme.is_active = False
        acme.save()
        with pytest.raises(DomainError):
            contracts.create_subcontract(
                actor=fin, project=project, subcontractor=acme, contract_value=D("5.00")
            )

    def test_a_value_change_is_audited_old_and_new(self, contract, pm):
        contracts.update_subcontract(contract, actor=pm, changes={"contract_value": D("120000")})

        contract.refresh_from_db()
        assert contract.contract_value == D("120000.00")
        row = AuditLog.objects.get(action=AuditAction.DOCUMENT_AMENDED)
        assert row.before == {"contract_value": "100000.00"}
        assert row.after == {"contract_value": "120000"}

    def test_an_unchanged_value_writes_no_audit_row(self, contract, pm):
        contracts.update_subcontract(contract, actor=pm, changes={"contract_value": D("100000")})
        assert not AuditLog.objects.filter(action=AuditAction.DOCUMENT_AMENDED).exists()

    def test_edit_sites_stay_a_subset(self, contract, pm, site2, stray):
        contracts.update_subcontract(contract, actor=pm, changes={"sites": [site2]})
        assert list(contract.sites.all()) == [site2]
        with pytest.raises(DomainError):
            contracts.update_subcontract(contract, actor=pm, changes={"sites": [stray]})

    def test_a_stranger_may_not_edit(self, contract, tech):
        with pytest.raises(PermissionDeniedError):
            contracts.update_subcontract(contract, actor=tech, changes={"payment_terms": "x"})


# --------------------------------------------------------------------------
# Services: jobs under a contract
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestJobLink:
    def link(self, project, sub, price, **extra):
        extra.setdefault("explicit", False)
        extra.setdefault("subcontract", None)
        return contracts.resolve_job_link(
            project=project, subcontractor=sub, agreed_price=D(price), **extra
        )

    def test_a_single_active_contract_is_chosen(self, project, acme, contract):
        assert self.link(project, acme, "1000") == (contract, "")

    def test_none_leaves_the_job_unlinked(self, project, other_sub, contract):
        assert self.link(project, other_sub, "1000") == (None, "")

    def test_a_closed_contract_is_not_auto_chosen(self, project, acme, contract, pm):
        contracts.update_subcontract(contract, actor=pm, changes={"status": "CLOSED"})
        assert self.link(project, acme, "1000") == (None, "")

    def test_two_active_contracts_are_ambiguous(self, project, acme, contract, fin):
        contracts.create_subcontract(
            actor=fin, project=project, subcontractor=acme, contract_value=D("5.00")
        )
        with pytest.raises(DomainError) as caught:
            self.link(project, acme, "1000")
        assert caught.value.code == "SUBCONTRACT_AMBIGUOUS"

    def test_choosing_one_resolves_the_ambiguity(self, project, acme, contract, fin):
        second = contracts.create_subcontract(
            actor=fin, project=project, subcontractor=acme, contract_value=D("50000")
        )
        assert self.link(project, acme, "1000", subcontract=second, explicit=True)[0] == second

    def test_the_wrong_subcontractor_is_a_mismatch(self, project, other_sub, contract):
        with pytest.raises(DomainError) as caught:
            self.link(project, other_sub, "1", subcontract=contract, explicit=True)
        assert caught.value.code == "SUBCONTRACT_MISMATCH"

    def test_the_wrong_project_is_a_mismatch(self, tenant, acme, contract, pm):
        elsewhere = po_project("WO-OTHER", pm)
        with pytest.raises(DomainError) as caught:
            self.link(elsewhere, acme, "1", subcontract=contract, explicit=True)
        assert caught.value.code == "SUBCONTRACT_MISMATCH"

    def test_a_job_with_no_project_cannot_be_under_a_contract(self, acme, contract):
        with pytest.raises(DomainError) as caught:
            self.link(None, acme, "1", subcontract=contract, explicit=True)
        assert caught.value.code == "SUBCONTRACT_MISMATCH"

    def test_over_the_value_needs_a_reason_and_records_it(
        self, tenant, project, acme, contract, site, pm
    ):
        make_job(tenant, project, site, pm, acme, "90000", link=contract)

        with pytest.raises(DomainError) as caught:
            self.link(project, acme, "10000.01")
        assert caught.value.code == "SUBCONTRACT_OVER_VALUE"

        assert self.link(project, acme, "10000.00") == (contract, "")
        assert self.link(project, acme, "20000", reason="Extra trench") == (
            contract,
            "Extra trench",
        )

    def test_cancelled_jobs_do_not_count_and_the_job_itself_is_excluded(
        self, tenant, project, acme, contract, site, pm
    ):
        make_job(
            tenant, project, site, pm, acme, "90000", status=JobStatus.CANCELLED, link=contract
        )
        mine = make_job(tenant, project, site, pm, acme, "100000", link=contract)

        assert (
            self.link(project, acme, "100000", job=mine, subcontract=contract, explicit=True)[1]
            == ""
        )


# --------------------------------------------------------------------------
# Services: payments
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestPayments:
    def test_finance_records_and_the_pm_is_asked(self, contract, fin, pm):
        payment = pay(contract, fin)

        assert payment.status == S.PENDING_PM
        request = ApprovalRequest.objects.get(document_id=str(payment.pk))
        assert request.required_user == pm

    def test_only_finance_records(self, contract, tech, pm):
        with pytest.raises(PermissionDeniedError):
            pay(contract, tech)
        with pytest.raises(PermissionDeniedError):
            pay(contract, pm)

    def test_the_recorder_who_is_the_pm_is_refused(self, tenant, project, contract, fin):
        project.manager = fin
        project.save()
        with pytest.raises(DomainError) as caught:
            pay(contract, fin)
        assert caught.value.code == "PAYMENT_NEEDS_OTHER_APPROVER"
        assert not SubcontractPayment.objects.exists()

    def test_an_inactive_pm_blocks_recording_and_leaves_nothing(self, contract, fin, pm):
        pm.is_active = False
        pm.save()
        with pytest.raises(ProjectHasNoActiveManager):
            pay(contract, fin)
        assert not SubcontractPayment.objects.exists()

    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"amount": "0"}, "FINANCE_INPUT_INVALID"),
            ({"amount": "-5"}, "FINANCE_INPUT_INVALID"),
            ({"paid_on": TODAY + timedelta(days=1)}, "FINANCE_INPUT_INVALID"),
            ({"reference": "  "}, "PAYMENT_REFERENCE_REQUIRED"),
        ],
    )
    def test_the_input_rules(self, contract, fin, kwargs, code):
        with pytest.raises(DomainError) as caught:
            pay(contract, fin, **kwargs)
        assert caught.value.code == code

    def test_a_repeated_client_uuid_is_one_payment(self, contract, fin):
        import uuid

        key = uuid.uuid4()
        first = pay(contract, fin, client_uuid=key)
        again = pay(contract, fin, client_uuid=key)

        assert first.pk == again.pk
        assert SubcontractPayment.objects.count() == 1

    def test_the_pm_approves_and_it_counts(self, contract, fin, pm):
        payment = approved_payment(contract, fin, pm)

        payment.refresh_from_db()
        assert payment.status == S.APPROVED
        assert payment.decided_by == pm
        assert contracts.position(contract).paid == D("1000.00")

    def test_the_recorder_may_not_decide(self, contract, fin):
        payment = pay(contract, fin)
        with pytest.raises(DomainError) as caught:
            contracts.decide(payment, actor=fin, approved=True)
        assert caught.value.code == "FINANCE_SELF_APPROVAL"

    def test_somebody_else_may_not_decide(self, contract, fin, fin2):
        payment = pay(contract, fin)
        with pytest.raises(DomainError):
            contracts.decide(payment, actor=fin2, approved=True)

    def test_a_rejection_needs_a_reason_and_goes_back(self, contract, fin, pm):
        payment = pay(contract, fin)
        with pytest.raises(DomainError) as caught:
            contracts.decide(payment, actor=pm, approved=False)
        assert caught.value.code == "REJECTION_REASON_REQUIRED"

        contracts.decide(payment, actor=pm, approved=False, reason="Wrong invoice")
        payment.refresh_from_db()
        assert payment.status == S.REJECTED
        assert payment.decision_reason == "Wrong invoice"
        assert contracts.position(contract).paid == 0

    def test_a_decided_payment_cannot_be_decided_again(self, contract, fin, pm):
        payment = approved_payment(contract, fin, pm)
        with pytest.raises(DomainError) as caught:
            contracts.decide(payment, actor=pm, approved=True)
        assert caught.value.code == "FINANCE_NOT_DECIDABLE"

    def test_resubmit_goes_round_again(self, contract, fin, pm):
        payment = pay(contract, fin)
        payment = contracts.decide(payment, actor=pm, approved=False, reason="Wrong invoice")

        contracts.resubmit(payment, actor=fin)

        payment.refresh_from_db()
        assert payment.status == S.PENDING_PM
        assert payment.decided_at is None
        assert ApprovalRequest.objects.filter(document_id=str(payment.pk)).count() == 2
        contracts.decide(payment, actor=pm, approved=True)
        payment.refresh_from_db()
        assert payment.status == S.APPROVED

    def test_only_the_recorder_resubmits_and_only_when_rejected(self, contract, fin, fin2, pm):
        payment = pay(contract, fin)
        with pytest.raises(DomainError):
            contracts.resubmit(payment, actor=fin)
        payment = contracts.decide(payment, actor=pm, approved=False, reason="No")
        with pytest.raises(PermissionDeniedError):
            contracts.resubmit(payment, actor=fin2)

    def test_reversal_is_negative_and_born_approved(self, contract, fin, pm):
        payment = approved_payment(contract, fin, pm, "1000.00")

        reversal = contracts.reverse_subcontract_payment(payment, actor=fin, reason="Paid twice")

        assert reversal.status == S.APPROVED
        assert reversal.reverses == payment
        assert reversal.signed_amount == D("-1000.00")
        assert contracts.position(contract).paid == 0
        row = AuditLog.objects.filter(target_id=str(reversal.pk)).first()
        assert row is not None

    def test_reversal_rules(self, contract, fin, pm, tech):
        pending = pay(contract, fin, reference="P")
        with pytest.raises(DomainError):
            contracts.reverse_subcontract_payment(pending, actor=pm, reason="x")
        payment = approved_payment(contract, fin, pm, reference="A")
        with pytest.raises(PermissionDeniedError):
            contracts.reverse_subcontract_payment(payment, actor=tech, reason="x")
        with pytest.raises(DomainError):
            contracts.reverse_subcontract_payment(payment, actor=pm, reason="")
        reversal = contracts.reverse_subcontract_payment(payment, actor=pm, reason="x")
        with pytest.raises(DomainError):
            contracts.reverse_subcontract_payment(payment, actor=pm, reason="again")
        with pytest.raises(DomainError):
            contracts.reverse_subcontract_payment(reversal, actor=pm, reason="x")


# --------------------------------------------------------------------------
# The worked example and the report
# --------------------------------------------------------------------------


@pytest.mark.django_db
class TestPositionAndReport:
    """Contract 100,000. Jobs: closed 30,000 and 20,000, open 25,000, cancelled 9,000.

    Payments: approved 40,000 and 15,000, pending 5,000, rejected 7,000, and a
    reversal of the 15,000.  Work done 50,000; committed 75,000; paid
    40,000 + 15,000 - 15,000 = 40,000; awaiting 5,000; owed 10,000.
    """

    @pytest.fixture
    def worked(self, tenant, project, acme, contract, site, fin, pm):
        make_job(tenant, project, site, pm, acme, "30000", status=JobStatus.CLOSED, link=contract)
        make_job(tenant, project, site, pm, acme, "20000", status=JobStatus.CLOSED, link=contract)
        make_job(tenant, project, site, pm, acme, "25000", link=contract)
        make_job(tenant, project, site, pm, acme, "9000", status=JobStatus.CANCELLED, link=contract)
        approved_payment(contract, fin, pm, "40000", "P1")
        second = approved_payment(contract, fin, pm, "15000", "P2")
        pay(contract, fin, "5000", "P3")
        contracts.decide(pay(contract, fin, "7000", "P4"), actor=pm, approved=False, reason="No")
        contracts.reverse_subcontract_payment(second, actor=fin, reason="Wrong")
        return contract

    def test_the_figures(self, worked):
        position = contracts.position(worked)

        assert position.work_done == D("50000.00")
        assert position.committed == D("75000.00")
        assert position.paid == D("40000.00")
        assert position.awaiting_approval == D("5000.00")
        assert position.owed == D("10000.00")
        assert not position.paid_exceeds_work_done
        assert not position.paid_exceeds_contract_value

    def test_an_advance_reads_negative_and_flags(self, worked, fin, pm):
        approved_payment(worked, fin, pm, "20000", "P5")

        position = contracts.position(worked)

        assert position.paid == D("60000.00")
        assert position.owed == D("-10000.00")
        assert position.paid_exceeds_work_done
        assert not position.paid_exceeds_contract_value

    def test_paying_past_the_value_flags(self, worked, fin, pm):
        approved_payment(worked, fin, pm, "70000", "P6")
        assert contracts.position(worked).paid_exceeds_contract_value

    def test_a_job_with_no_contract_is_not_in_the_position(
        self, tenant, project, acme, site, pm, worked
    ):
        make_job(tenant, project, site, pm, acme, "5000", status=JobStatus.CLOSED)
        assert contracts.position(worked).work_done == D("50000.00")

    def rows(self, **params):
        return {row["name"]: row for row in SubcontractorSpendReport().rows(params)}

    def test_the_report_gains_paid_and_owed(self, worked):
        row = self.rows()["Acme Civils"]

        assert row["delivered"] == D("50000.00")
        assert row["paid"] == D("40000.00")
        assert row["owed"] == D("10000.00")
        assert row["committed"] == D("25000.00")

    def test_the_subcontract_filter(self, worked, tenant, project, site, pm, other_sub, fin):
        other = contracts.create_subcontract(
            actor=fin, project=project, subcontractor=other_sub, contract_value=D("1000")
        )
        make_job(tenant, project, site, pm, other_sub, "700", status=JobStatus.CLOSED, link=other)

        only = self.rows(subcontract=worked.pk)

        assert list(only) == ["Acme Civils"]
        assert only["Acme Civils"]["paid"] == D("40000.00")
        assert self.rows(subcontract=other.pk)["Other Builders"]["owed"] == D("700.00")

    def test_delivered_work_with_no_contract_reads_as_owed(
        self, tenant, project, other_sub, site, pm
    ):
        make_job(tenant, project, site, pm, other_sub, "800", status=JobStatus.CLOSED)

        row = self.rows()["Other Builders"]

        assert (row["delivered"], row["paid"], row["owed"]) == (D("800"), D("0"), D("800"))

    def test_the_project_filter_scopes_paid(self, worked, tenant, acme, pm):
        elsewhere = po_project("WO-ELSE", pm)

        assert self.rows(project=elsewhere.pk).get("Acme Civils", {"paid": 0})["paid"] == 0
        assert self.rows(project=worked.project_id)["Acme Civils"]["paid"] == D("40000.00")

    def test_totals_carry_paid_and_owed(self, worked):
        report = SubcontractorSpendReport()
        rows = list(report.rows({}))
        totals = report.totals(rows)

        assert totals["paid"] == D("40000.00")
        assert totals["owed"] == D("10000.00")


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


def contract_body(project, sub, sites, **extra):
    return {
        "project": project.pk,
        "subcontractor": sub.pk,
        "sites": [s.pk for s in sites],
        "contract_value": "100000.00",
        "payment_terms": "30 days",
        **extra,
    }


@pytest.mark.django_db
class TestContractEndpoints:
    def test_finance_creates_and_reads_the_detail(self, client, project, acme, fin, site):
        http = Api(client, fin)

        response = http.post("subcontracts", contract_body(project, acme, [site]))

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["reference"].startswith("SC")
        assert body["sites"] == [site.pk]
        assert body["subcontractor_name"] == "Acme Civils"
        assert body["status"] == "ACTIVE"
        assert body["position"]["owed"] == "0.00"
        assert body["jobs"] == [] and body["payments"] == []

    def test_the_pm_creates_and_edits(self, client, project, acme, pm, site, site2):
        http = Api(client, pm)
        made = http.post("subcontracts", contract_body(project, acme, [site])).json()

        response = http.patch(
            f"subcontracts/{made['id']}",
            {"contract_value": "150000.00", "payment_terms": "60 days", "sites": [site2.pk]},
        )

        assert response.status_code == 200, response.content
        assert response.json()["contract_value"] == "150000.00"
        assert response.json()["sites"] == [site2.pk]
        assert response.json()["payment_terms"] == "60 days"

    def test_a_stranger_cannot_create_or_edit(self, client, project, acme, tech, site, contract):
        http = Api(client, tech)

        assert http.post("subcontracts", contract_body(project, acme, [site])).status_code == 403
        assert http.patch(f"subcontracts/{contract.pk}", {"payment_terms": "x"}).status_code == 404

    def test_a_pm_may_not_edit_another_projects_contract(self, client, tenant, contract):
        other_pm = person(tenant, "Other PM")
        po_project("WO-OPM", other_pm)

        assert Api(client, other_pm).get(f"subcontracts/{contract.pk}").status_code == 404

    def test_sites_off_the_project_are_a_400(self, client, project, acme, fin, stray):
        response = Api(client, fin).post("subcontracts", contract_body(project, acme, [stray]))

        assert response.status_code == 400
        assert "sites" in response.json()["error"]["field_errors"]

    def test_the_list_filters_by_project_and_carries_the_position(self, client, contract, fin, pm):
        for user in (fin, pm):
            response = Api(client, user).get("subcontracts", project=contract.project_id)
            rows = results(response)
            assert [r["id"] for r in rows] == [contract.pk]
            assert set(rows[0]["position"]) >= {
                "contract_value",
                "work_done",
                "paid",
                "awaiting_approval",
                "owed",
                "committed",
                "paid_exceeds_work_done",
                "paid_exceeds_contract_value",
            }
        empty = po_project("WO-EMPTY", pm)
        assert results(Api(client, fin).get("subcontracts", project=empty.pk)) == []

    def test_the_detail_lists_jobs_and_payments(
        self, client, tenant, project, acme, contract, site, fin, pm
    ):
        make_job(tenant, project, site, pm, acme, "30000", status=JobStatus.CLOSED, link=contract)
        approved_payment(contract, fin, pm, "10000")

        body = Api(client, pm).get(f"subcontracts/{contract.pk}").json()

        assert [j["status"] for j in body["jobs"]] == ["CLOSED"]
        assert body["jobs"][0]["agreed_price"] == "30000.00"
        assert [p["amount"] for p in body["payments"]] == ["10000.00"]
        assert body["position"]["work_done"] == "30000.00"
        assert body["position"]["owed"] == "20000.00"

    def test_an_unrelated_member_sees_none(self, client, contract, tech):
        assert results(Api(client, tech).get("subcontracts")) == []
        assert Api(client, tech).get(f"subcontracts/{contract.pk}").status_code == 404

    def test_a_page_costs_a_fixed_number_of_queries(
        self, client, django_assert_max_num_queries, project, acme, fin, site
    ):
        for _ in range(4):
            contracts.create_subcontract(
                actor=fin, project=project, subcontractor=acme, contract_value=D("5"), sites=[site]
            )
        http = Api(client, fin)
        with django_assert_max_num_queries(25):
            assert len(results(http.get("subcontracts"))) == 4


@pytest.mark.django_db
class TestPaymentEndpoints:
    def body(self, contract, **extra):
        return {
            "subcontract": contract.pk,
            "amount": "5000.00",
            "paid_on": TODAY.isoformat(),
            "reference": "INV-77",
            **extra,
        }

    def test_finance_records_and_the_response_reads(self, client, contract, fin):
        response = Api(client, fin).post("subcontract-payments", self.body(contract))

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["status"] == "PENDING_PM"
        assert body["amount"] == "5000.00"
        assert body["reference"] == "INV-77"
        assert body["subcontract_number"] == contract.number
        assert body["subcontractor_name"] == "Acme Civils"
        assert body["project_reference"] == "WO-1808"
        assert body["recorded_by_name"] == "Fiona Finance"
        assert body["would_exceed_work_done"] is True

    def test_only_finance_records(self, client, contract, pm, tech):
        assert Api(client, pm).post("subcontract-payments", self.body(contract)).status_code == 403
        assert (
            Api(client, tech).post("subcontract-payments", self.body(contract)).status_code == 403
        )

    def test_a_pm_recorder_is_a_409(self, client, tenant, project, contract):
        pm_fin = person(tenant, "Pat PM Finance", PERM.FINANCE_APPROVE)
        project.manager = pm_fin
        project.save()

        response = Api(client, pm_fin).post("subcontract-payments", self.body(contract))

        assert response.status_code == 409
        assert error_code(response) == "PAYMENT_NEEDS_OTHER_APPROVER"

    def test_a_missing_reference_is_the_payment_reference_code(self, client, contract, fin):
        response = Api(client, fin).post("subcontract-payments", self.body(contract, reference=""))

        assert response.status_code == 400
        assert error_code(response) == "PAYMENT_REFERENCE_REQUIRED"

    def test_an_unknown_subcontract_is_a_400(self, client, fin):
        response = Api(client, fin).post(
            "subcontract-payments",
            {"subcontract": 999999, "amount": "1", "paid_on": TODAY.isoformat(), "reference": "x"},
        )
        assert response.status_code == 400

    def test_the_pms_pending_list_and_decide(self, client, contract, fin, pm, fin2):
        payment = pay(contract, fin)

        pending = results(Api(client, pm).get("subcontract-payments", pending="true"))
        assert [p["id"] for p in pending] == [payment.pk]
        assert pending[0]["position"]["owed"] == "0.00"
        assert results(Api(client, fin2).get("subcontract-payments", pending="true")) == []

        response = Api(client, pm).post(
            f"subcontract-payments/{payment.pk}/decide", {"approved": True}
        )

        assert response.status_code == 200, response.content
        assert response.json()["status"] == "APPROVED"
        assert results(Api(client, pm).get("subcontract-payments", pending="true")) == []

    def test_the_recorder_cannot_decide_and_a_reject_needs_a_reason(
        self, client, contract, fin, pm
    ):
        payment = pay(contract, fin)

        mine = Api(client, fin).post(
            f"subcontract-payments/{payment.pk}/decide", {"approved": True}
        )
        assert mine.status_code == 403
        assert error_code(mine) == "FINANCE_SELF_APPROVAL"
        bare = Api(client, pm).post(
            f"subcontract-payments/{payment.pk}/decide", {"approved": False}
        )
        assert bare.status_code == 400
        assert error_code(bare) == "REJECTION_REASON_REQUIRED"

    def test_reject_then_resubmit(self, client, contract, fin, pm):
        payment = pay(contract, fin)
        Api(client, pm).post(
            f"subcontract-payments/{payment.pk}/decide", {"approved": False, "reason": "No invoice"}
        )

        rejected = Api(client, fin).get(f"subcontract-payments/{payment.pk}").json()
        assert rejected["status"] == "REJECTED"
        assert rejected["rejection_reason"] == "No invoice"

        again = Api(client, fin).post(f"subcontract-payments/{payment.pk}/resubmit")
        assert again.status_code == 200
        assert again.json()["status"] == "PENDING_PM"

    def test_reverse_shows_negative_with_its_original(self, client, contract, fin, pm):
        payment = approved_payment(contract, fin, pm, "1000")

        response = Api(client, fin).post(
            f"subcontract-payments/{payment.pk}/reverse", {"reason": "Paid twice"}
        )

        assert response.status_code == 201, response.content
        assert response.json()["amount"] == "-1000.00"
        assert response.json()["reverses"] == payment.pk
        listed = results(Api(client, pm).get("subcontract-payments", subcontract=contract.pk))
        assert sorted(p["amount"] for p in listed) == ["-1000.00", "1000.00"]

    def test_visibility(self, client, contract, fin, pm, tech):
        pay(contract, fin)

        assert len(results(Api(client, fin).get("subcontract-payments"))) == 1
        assert len(results(Api(client, pm).get("subcontract-payments"))) == 1
        assert results(Api(client, tech).get("subcontract-payments")) == []

    def test_no_edit_or_delete(self, client, contract, fin):
        payment = pay(contract, fin)
        http = Api(client, fin)
        assert http.patch(f"subcontract-payments/{payment.pk}", {"amount": "1"}).status_code == 405
        assert http.delete(f"subcontract-payments/{payment.pk}").status_code == 405


@pytest.mark.django_db
class TestJobEndpoints:
    def job_body(self, project, site, who, sub, price="1000.00", **extra):
        return {
            "client": site.client_id,
            "site": site.pk,
            "project": project.pk,
            "assignee": who.pk,
            "description": "Trench",
            "delivery_mode": "SUBCONTRACTED",
            "subcontractor": sub.pk,
            "agreed_price": price,
            **extra,
        }

    def test_a_single_contract_is_linked_automatically(
        self, client, project, site, jobber, tech, acme, contract
    ):
        response = Api(client, jobber).post("jobs", self.job_body(project, site, tech, acme))

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["subcontract"] == contract.pk
        assert body["subcontract_reference"] == contract.number
        assert body["over_contract_reason"] == ""

    def test_no_contract_stays_unlinked(self, client, project, site, jobber, tech, acme):
        response = Api(client, jobber).post("jobs", self.job_body(project, site, tech, acme))

        assert response.status_code == 201
        assert response.json()["subcontract"] is None

    def test_ambiguity_is_a_400_until_one_is_chosen(
        self, client, project, site, jobber, tech, acme, contract, fin
    ):
        second = contracts.create_subcontract(
            actor=fin, project=project, subcontractor=acme, contract_value=D("1000000")
        )
        http = Api(client, jobber)

        refused = http.post("jobs", self.job_body(project, site, tech, acme))
        chosen = http.post("jobs", self.job_body(project, site, tech, acme, subcontract=second.pk))

        assert refused.status_code == 400
        assert error_code(refused) == "SUBCONTRACT_AMBIGUOUS"
        assert chosen.status_code == 201
        assert chosen.json()["subcontract"] == second.pk

    def test_over_value_needs_the_reason_then_records_it(
        self, client, project, site, jobber, tech, acme, contract
    ):
        http = Api(client, jobber)

        refused = http.post("jobs", self.job_body(project, site, tech, acme, "100000.01"))
        recorded = http.post(
            "jobs",
            self.job_body(
                project, site, tech, acme, "100000.01", over_contract_reason="Extra trench"
            ),
        )

        assert refused.status_code == 400
        assert error_code(refused) == "SUBCONTRACT_OVER_VALUE"
        assert recorded.status_code == 201
        assert recorded.json()["over_contract_reason"] == "Extra trench"

    def test_a_mismatched_contract_is_a_400(
        self, client, project, site, jobber, tech, other_sub, contract
    ):
        response = Api(client, jobber).post(
            "jobs", self.job_body(project, site, tech, other_sub, subcontract=contract.pk)
        )

        assert response.status_code == 400
        assert error_code(response) == "SUBCONTRACT_MISMATCH"

    def test_an_in_house_job_carries_no_contract(
        self, client, project, site, jobber, tech, contract, acme
    ):
        body = {
            "client": site.client_id,
            "site": site.pk,
            "project": project.pk,
            "assignee": tech.pk,
            "description": "Own crew",
            "delivery_mode": "IN_HOUSE",
        }
        response = Api(client, jobber).post("jobs", body)

        assert response.status_code == 201
        assert response.json()["subcontract"] is None

    def test_repricing_over_the_value_needs_a_reason(
        self, client, tenant, project, site, jobber, tech, acme, contract
    ):
        job = make_job(tenant, project, site, tech, acme, "1000", link=contract)
        http = Api(client, jobber)

        refused = http.patch(f"jobs/{job.pk}", {"agreed_price": "200000.00"})
        allowed = http.patch(
            f"jobs/{job.pk}", {"agreed_price": "200000.00", "over_contract_reason": "Scope grew"}
        )

        assert error_code(refused) == "SUBCONTRACT_OVER_VALUE"
        assert allowed.status_code == 200
        assert allowed.json()["over_contract_reason"] == "Scope grew"

    def test_editing_the_description_does_not_pull_an_old_job_under_a_contract(
        self, client, tenant, project, site, jobber, tech, acme, contract
    ):
        job = make_job(tenant, project, site, tech, acme, "1000")

        response = Api(client, jobber).patch(f"jobs/{job.pk}", {"description": "Renamed"})

        assert response.status_code == 200
        assert response.json()["subcontract"] is None

    def test_a_closed_jobs_link_is_frozen(
        self, client, tenant, project, site, jobber, tech, acme, contract
    ):
        job = make_job(
            tenant, project, site, tech, acme, "1000", status=JobStatus.CLOSED, link=contract
        )

        response = Api(client, jobber).patch(f"jobs/{job.pk}", {"subcontract": None})

        assert response.status_code == 400


# --------------------------------------------------------------------------
# Attachments
# --------------------------------------------------------------------------


def upload(http, target_type, target_id, caption):
    return http.upload(
        {
            "target_type": target_type,
            "target_id": str(target_id),
            "file": SimpleUploadedFile("f.pdf", b"%PDF- pretend", content_type="application/pdf"),
            "kind": "DOCUMENT",
            "caption": caption,
        }
    )


@pytest.mark.django_db
class TestAttachments:
    def test_the_contract_takes_a_document_from_its_pm_or_finance(
        self, client, contract, pm, fin, tech
    ):
        assert (
            upload(Api(client, pm), "commercials.Subcontract", contract.pk, "Contract").status_code
            == 201
        )
        assert (
            upload(Api(client, fin), "commercials.Subcontract", contract.pk, "Contract").status_code
            == 201
        )
        assert (
            upload(
                Api(client, tech), "commercials.Subcontract", contract.pk, "Contract"
            ).status_code
            == 403
        )

    def test_the_contract_stays_replaceable_after_payments(self, client, contract, fin, pm):
        approved_payment(contract, fin, pm)

        response = upload(Api(client, pm), "commercials.Subcontract", contract.pk, "Contract")

        assert response.status_code == 201
        assert Attachment.objects.filter(target_type="commercials.Subcontract").count() == 1

    def test_the_invoice_is_its_recorders_while_open(self, client, contract, fin, fin2, pm):
        payment = pay(contract, fin)
        target = "commercials.SubcontractPayment"

        assert upload(Api(client, fin), target, payment.pk, "Invoice").status_code == 201
        assert upload(Api(client, fin2), target, payment.pk, "Invoice").status_code == 403
        assert upload(Api(client, pm), target, payment.pk, "Invoice").status_code == 403

    def test_the_invoice_is_fixed_once_approved(self, client, contract, fin, pm):
        payment = pay(contract, fin)
        target = "commercials.SubcontractPayment"
        added = upload(Api(client, fin), target, payment.pk, "Invoice")
        contracts.decide(payment, actor=pm, approved=True)

        assert upload(Api(client, fin), target, payment.pk, "Invoice").status_code == 409
        assert Api(client, fin).delete(f"attachments/{added.json()['id']}").status_code == 409

    def test_a_rejected_payment_may_gain_its_invoice(self, client, contract, fin, pm):
        payment = pay(contract, fin)
        contracts.decide(payment, actor=pm, approved=False, reason="No invoice")

        response = upload(Api(client, fin), "commercials.SubcontractPayment", payment.pk, "Invoice")

        assert response.status_code == 201
