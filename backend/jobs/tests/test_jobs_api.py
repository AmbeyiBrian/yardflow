"""T5.5, T5.7 and the Phase 5 endpoints (§6, §4.9, §4.10; H2–H5, I1–I5, M1).

Two of these carry a stated criterion of their own:

* T5.5 — "one endpoint answers *what is currently unresolved*"
* T5.7 — the reconciliation figures reach the API the screens read

The rest are the refusals: status is not writable, a variance cannot be invented,
and a handover cannot be acknowledged by the person giving it away.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from core.provisioning import provision_tenant
from core.tenancy import tenant_context


@pytest.fixture
def signed_in(db, client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    owner = result["owner"]
    owner.set_password("a good long password")
    owner.save()

    client.defaults["HTTP_HOST"] = "silvertech.localhost"
    token = client.post(
        reverse("v1:auth:login"),
        {"identifier": "owner@silvertech.co.ke", "password": "a good long password"},
        content_type="application/json",
    ).json()["access"]
    return client, token, result["organization"], owner


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def a_job(organization, assignee):
    from jobs.models import Job
    from network.factories import SiteFactory

    site = SiteFactory(internal_ref="API-1001", name="Api site")
    return Job.objects.create(
        organization=organization,
        reference="JOB-API-1",
        client=site.client,
        site=site,
        assignee=assignee,
    )


class TestJobEndpoints:
    def test_a_job_can_be_raised_and_listed(self, signed_in):
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from network.factories import SiteFactory

            site = SiteFactory(internal_ref="API-2001", name="Listed site")

        response = http.post(
            reverse("v1:job-list"),
            {
                "reference": "JOB-API-2",
                "client": site.client_id,
                "site": site.pk,
                "assignee": owner.pk,
                "description": "Swap the RRU",
            },
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 201, response.content
        assert response.json()["site_name"] == "Listed site"
        assert response.json()["status"] == "OPEN"

    def test_status_cannot_be_patched(self, signed_in):
        """§6: state changes are POST actions, never a PATCH on status.

        A job that could be marked closed directly would walk straight past H5's
        guard, which is the only thing making a closed job mean anything.
        """
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            job = a_job(organization, owner)

        response = http.patch(
            reverse("v1:job-detail", args=[job.pk]),
            {"status": "CLOSED"},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200
        job.refresh_from_db()
        assert job.status == "OPEN"

    def test_closing_a_clean_job_succeeds(self, signed_in):
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            job = a_job(organization, owner)

        response = http.post(
            reverse("v1:job-close", args=[job.pk]),
            {"reason": ""},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
        assert response.json()["status"] == "CLOSED"
        assert response.json()["closed_with_variance"] is False

    def test_closing_a_job_with_material_out_is_refused(self, signed_in):
        """H5, through the API: the guard is not advisory."""
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from catalogue.factories import ItemTypeFactory
            from custody.models import CustodyExpectation

            job = a_job(organization, owner)
            CustodyExpectation.objects.create(
                organization=organization,
                holder=owner,
                item_type=ItemTypeFactory(name="Api torque wrench"),
                quantity=Decimal("1"),
            )

        # The owner role holds job.close_with_variance, so the override is
        # available — but it still requires a reason.
        response = http.post(
            reverse("v1:job-close", args=[job.pk]),
            {"reason": ""},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "JOB_HAS_UNACCOUNTED_MATERIAL"

        with_reason = http.post(
            reverse("v1:job-close", args=[job.pk]),
            {"reason": "Wrench written off, deducted from the subcontractor."},
            content_type="application/json",
            **auth(token),
        )

        assert with_reason.status_code == 200, with_reason.content
        assert with_reason.json()["closed_with_variance"] is True

    def test_a_closeout_is_submitted_in_one_call(self, signed_in):
        """H2: lines arrive with the closeout, not one by one.

        A closeout assembled line by line over a bad connection can be submitted
        half-built, and half a closeout posts half the movements.
        """
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from catalogue.factories import ItemTypeFactory
            from locations.factories import YardFactory
            from locations.nodes import external_node, node_for_user
            from stock.models import MovementType
            from stock.services import MovementRequest, post_movement

            job = a_job(organization, owner)
            item = ItemTypeFactory(name="Api feeder clamp")
            yard = YardFactory(name="Api yard")
            # Put it in the technician's hands first, or there is nothing to
            # report installing.
            from django.db import transaction

            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=item,
                        quantity=Decimal("5"),
                        from_node=external_node(organization.pk),
                        to_node=node_for_user(owner),
                        movement_type=MovementType.RECEIPT,
                    )
                )
            assert yard is not None

        created = http.post(
            reverse("v1:job-closeout-list"),
            {
                "job": job.pk,
                "lines": [
                    {
                        "action": "INSTALLED",
                        "item_type": item.pk,
                        "quantity": "2",
                        "uom": item.uom,
                    },
                    {
                        "action": "RETURNING",
                        "item_type": item.pk,
                        "quantity": "3",
                        "uom": item.uom,
                    },
                ],
            },
            content_type="application/json",
            **auth(token),
        )

        assert created.status_code == 201, created.content
        closeout_id = created.json()["id"]
        assert len(created.json()["lines"]) == 2

        submitted = http.post(
            reverse("v1:job-closeout-submit", args=[closeout_id]),
            content_type="application/json",
            **auth(token),
        )

        assert submitted.status_code == 200, submitted.content
        with tenant_context(organization):
            from locations.nodes import node_for_site
            from stock.services import balance_at

            # Installed posted; the declared return did not move (§4.9).
            assert balance_at(node_for_site(job.site), item) == Decimal("2")
            assert balance_at(node_for_user(owner), item) == Decimal("3")


class TestReconciliationEndpoint:
    """T5.7: the figures the screens read."""

    def test_a_site_reconciles_over_the_api(self, signed_in):
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            job = a_job(organization, owner)

        response = http.get(
            reverse("v1:reconciliation"), {"site": job.site_id}, **auth(token)
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["scope"] == "site"
        assert body["is_reconciled"] is True

    def test_naming_neither_is_a_validation_error(self, signed_in):
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:reconciliation"), **auth(token))

        assert response.status_code == 400

    def test_another_tenants_site_is_not_found(self, signed_in):
        """A3: 404, so an id is never confirmed."""
        http, token, _organization, _owner = signed_in

        rival = provision_tenant(name="Rival", slug="rival", owner_email="o@rival.co.ke")
        with tenant_context(rival["organization"]):
            from network.factories import SiteFactory

            rival_site = SiteFactory(internal_ref="RIV-1", name="Rival site")

        response = http.get(
            reverse("v1:reconciliation"), {"site": rival_site.pk}, **auth(token)
        )

        assert response.status_code == 404


class TestExceptionsRegister:
    """T5.5: one endpoint answers "what is currently unresolved"."""

    def test_it_combines_variances_release_variances_and_overdue_custody(
        self, signed_in
    ):
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from datetime import timedelta

            from django.utils import timezone

            from catalogue.factories import ItemTypeFactory
            from custody.models import CustodyExpectation, ExpectationStatus
            from dispatch.models import GateOut, GateOutLine, GateOutPurpose, ReleaseVariance
            from jobs.models import Variance, VarianceType
            from locations.factories import YardFactory
            from network.factories import SiteFactory

            item = ItemTypeFactory(name="Register item")

            Variance.objects.create(
                organization=organization,
                type=VarianceType.RETURN,
                item_type=item,
                expected=Decimal("3"),
                actual=Decimal("2"),
                uom=item.uom,
                reason="One jumper short.",
            )

            yard = YardFactory(name="Register yard")
            site = SiteFactory(internal_ref="REG-1", name="Register site")
            gate_out = GateOut.objects.create(
                organization=organization,
                from_location=yard,
                site=site,
                custody_holder=owner,
                requested_by=owner,
                purpose_type=GateOutPurpose.INSTALLATION,
                number="GP-REG-1",
            )
            line = GateOutLine.objects.create(
                organization=organization,
                gate_out=gate_out,
                item_type=item,
                tracking_mode=item.default_tracking_mode,
                requested_qty=Decimal("4"),
                uom=item.uom,
            )
            ReleaseVariance.objects.create(
                organization=organization,
                gate_out_line=line,
                approved_qty=Decimal("4"),
                released_qty=Decimal("3"),
                reason="Only three on the shelf.",
            )

            CustodyExpectation.objects.create(
                organization=organization,
                holder=owner,
                item_type=item,
                quantity=Decimal("1"),
                expected_return_date=timezone.now().date() - timedelta(days=5),
                status=ExpectationStatus.OVERDUE,
            )

        response = http.get(reverse("v1:exceptions"), **auth(token))

        assert response.status_code == 200, response.content
        body = response.json()
        kinds = {entry["kind"] for entry in body["items"]}
        assert kinds == {"variance", "release_variance", "custody"}
        assert body["count"] == 3

    def test_it_can_be_narrowed_to_one_kind(self, signed_in):
        http, token, organization, _owner = signed_in

        with tenant_context(organization):
            from catalogue.factories import ItemTypeFactory
            from jobs.models import Variance, VarianceType

            Variance.objects.create(
                organization=organization,
                type=VarianceType.COUNT,
                item_type=ItemTypeFactory(name="Narrowed item"),
                expected=Decimal("10"),
                actual=Decimal("9"),
                reason="Count short.",
            )

        response = http.get(reverse("v1:exceptions"), {"kind": "custody"}, **auth(token))

        assert response.status_code == 200
        assert response.json()["count"] == 0

    def test_a_resolved_variance_leaves_the_register(self, signed_in):
        """H3: "variances appear on an exceptions report **until resolved**"."""
        http, token, organization, _owner = signed_in

        with tenant_context(organization):
            from catalogue.factories import ItemTypeFactory
            from jobs.models import Variance, VarianceType

            variance = Variance.objects.create(
                organization=organization,
                type=VarianceType.RETURN,
                item_type=ItemTypeFactory(name="Resolvable item"),
                expected=Decimal("2"),
                actual=Decimal("1"),
                reason="One short.",
            )

        assert http.get(reverse("v1:exceptions"), **auth(token)).json()["count"] == 1

        resolved = http.post(
            reverse("v1:variance-resolve", args=[variance.pk]),
            {"resolution": "Found behind the container.", "write_off": False},
            content_type="application/json",
            **auth(token),
        )

        assert resolved.status_code == 200, resolved.content
        assert http.get(reverse("v1:exceptions"), **auth(token)).json()["count"] == 0

    def test_resolving_without_an_explanation_is_refused(self, signed_in):
        http, token, organization, _owner = signed_in

        with tenant_context(organization):
            from catalogue.factories import ItemTypeFactory
            from jobs.models import Variance, VarianceType

            variance = Variance.objects.create(
                organization=organization,
                type=VarianceType.RETURN,
                item_type=ItemTypeFactory(name="Unexplained item"),
                expected=Decimal("2"),
                actual=Decimal("1"),
            )

        response = http.post(
            reverse("v1:variance-resolve", args=[variance.pk]),
            {"resolution": ""},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400

    def test_a_variance_cannot_be_created_by_hand(self, signed_in):
        """A register anyone can write into carries no weight."""
        http, token, _organization, _owner = signed_in

        response = http.post(
            reverse("v1:variance-list"),
            {"type": "RETURN", "expected": "1", "actual": "0"},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 405


class TestCustodyEndpoints:
    def test_holdings_default_to_the_caller(self, signed_in):
        """I1, §7.4: "my custody" needs no permission of its own."""
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from django.db import transaction

            from catalogue.factories import ItemTypeFactory
            from locations.nodes import external_node, node_for_user
            from stock.models import MovementType
            from stock.services import MovementRequest, post_movement

            item = ItemTypeFactory(name="Held item")
            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=item,
                        quantity=Decimal("2"),
                        from_node=external_node(organization.pk),
                        to_node=node_for_user(owner),
                        movement_type=MovementType.RECEIPT,
                    )
                )

        response = http.get(reverse("v1:custody-holdings"), **auth(token))

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["holder_id"] == owner.pk
        assert body["items"][0]["item"] == "Held item"
        assert body["items"][0]["quantity"] == "2.000"

    def test_a_handover_moves_nothing_until_acknowledged(self, signed_in):
        """I5, over the API."""
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from django.db import transaction

            from accounts.factories import UserFactory
            from catalogue.factories import ItemTypeFactory
            from locations.nodes import external_node, node_for_user
            from stock.models import MovementType
            from stock.services import MovementRequest, balance_at, post_movement

            receiver = UserFactory(organization=organization, full_name="Receiving Rita")
            item = ItemTypeFactory(name="Handover item")
            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=item,
                        quantity=Decimal("1"),
                        from_node=external_node(organization.pk),
                        to_node=node_for_user(owner),
                        movement_type=MovementType.RECEIPT,
                    )
                )

        created = http.post(
            reverse("v1:custody-transfer-list"),
            {
                "from_holder": owner.pk,
                "to_holder": receiver.pk,
                "lines": [
                    {"item_type": item.pk, "quantity": "1", "uom": item.uom}
                ],
            },
            content_type="application/json",
            **auth(token),
        )

        assert created.status_code == 201, created.content
        assert created.json()["status"] == "PENDING"

        with tenant_context(organization):
            assert balance_at(node_for_user(receiver), item) == Decimal("0")

        # The giver cannot acknowledge on the receiver's behalf — that is the
        # whole of I5.
        refused = http.post(
            reverse("v1:custody-transfer-acknowledge", args=[created.json()["id"]]),
            content_type="application/json",
            **auth(token),
        )

        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "TRANSFER_NOT_READY"

    def test_the_overdue_report_groups_both_ways(self, signed_in):
        """I4: who keeps doing this, and what do we keep losing."""
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from datetime import timedelta

            from django.utils import timezone

            from catalogue.factories import ItemTypeFactory
            from custody.models import CustodyExpectation, ExpectationStatus

            CustodyExpectation.objects.create(
                organization=organization,
                holder=owner,
                item_type=ItemTypeFactory(name="Overdue item"),
                quantity=Decimal("1"),
                expected_return_date=timezone.now().date() - timedelta(days=9),
                status=ExpectationStatus.OVERDUE,
            )

        response = http.get(reverse("v1:custody-overdue"), **auth(token))

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["total"] == 1
        assert body["by_person"][0]["holder"] == str(owner)
        assert body["by_item"][0]["item"] == "Overdue item"
        # T5.11 opens a holder's holdings from this row, and shows how late the
        # worst line is — so both travel with it (I4).
        assert body["by_person"][0]["holder_id"] == owner.pk
        assert body["by_person"][0]["days_overdue"] == 9


class TestWhatIAmCarrying:
    """The technician screens' one new read (T5.10, I1).

    A serialized unit and a drum are located by ``current_node``, and a person's
    node does not exist until their first custody (§3.1) — so "what am I
    carrying?" cannot be a filter on a node id the phone does not have.
    """

    def test_serials_can_be_filtered_by_holder(self, signed_in):
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from catalogue.factories import ItemTypeFactory
            from locations.nodes import node_for_user
            from stock.models import SerialUnit

            item = ItemTypeFactory(name="Held radio")
            SerialUnit.objects.create(
                organization=organization,
                item_type=item,
                serial_number="HELD-1",
                current_node=node_for_user(owner),
            )
            from locations.factories import YardFactory

            SerialUnit.objects.create(
                organization=organization,
                item_type=item,
                serial_number="YARD-1",
                current_node=YardFactory().node,
            )

        response = http.get(reverse("v1:serial-list"), {"holder": "me"}, **auth(token))

        assert response.status_code == 200, response.content
        assert [row["serial_number"] for row in response.json()["results"]] == ["HELD-1"]

    def test_holding_nothing_is_an_empty_list_not_a_failure(self, signed_in):
        """A technician's first render happens before they have ever held stock."""
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:drum-list"), {"holder": "me"}, **auth(token))

        assert response.status_code == 200
        assert response.json()["results"] == []


class TestMyJobsList:
    """T5.10's list. Found in the browser, not by a test: a technician's screen
    that filtered on ``status=OPEN`` showed nothing, because the job waiting to
    be closed out was ``AWAITING_CLOSEOUT`` — the exact list they came for.
    """

    def test_open_covers_every_status_still_to_be_closed(self, signed_in):
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from jobs.models import Job, JobStatus
            from network.factories import SiteFactory

            site = SiteFactory(internal_ref="MINE-1", name="Mine")
            from django.utils import timezone

            for index, status in enumerate(
                (
                    JobStatus.OPEN,
                    JobStatus.IN_PROGRESS,
                    JobStatus.AWAITING_CLOSEOUT,
                    JobStatus.CLOSED,
                    JobStatus.CANCELLED,
                )
            ):
                Job.objects.create(
                    organization=organization,
                    reference=f"MINE-{index}",
                    client=site.client,
                    site=site,
                    assignee=owner,
                    status=status,
                    # A closed job records when, and the database enforces it.
                    closed_at=(
                        timezone.now() if status == JobStatus.CLOSED else None
                    ),
                    closed_by=owner if status == JobStatus.CLOSED else None,
                )

        response = http.get(
            reverse("v1:job-list"),
            {"open": "true", "assignee": "me", "page_size": 50},
            **auth(token),
        )

        assert response.status_code == 200, response.content
        statuses = {row["status"] for row in response.json()["results"]}
        assert statuses == {"OPEN", "IN_PROGRESS", "AWAITING_CLOSEOUT"}

    def test_assignee_still_accepts_an_id(self, signed_in):
        """An owner's screen names a person; a technician's says "me"."""
        http, token, organization, owner = signed_in
        with tenant_context(organization):
            a_job(organization, owner)

        response = http.get(
            reverse("v1:job-list"), {"assignee": owner.pk}, **auth(token)
        )

        assert response.status_code == 200, response.content
        assert response.json()["results"]
