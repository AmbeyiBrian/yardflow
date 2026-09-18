"""T5.1–T5.9 — jobs, closeout, reconciliation and custody (§4.9, §4.10; H1–H5, I1–I5).

The test that matters most is the full lifecycle one: issue, install, consume,
declare a return, receive it, reconcile. §14 asks for exactly that —
"issued vs installed vs returned vs unaccounted sums correctly across a full job
lifecycle" — because four figures that disagree is what would end an operator
conversation badly.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from custody.models import (
    CustodyExpectation,
    CustodyTransfer,
    CustodyTransferLine,
    ExpectationStatus,
    TransferStatus,
)
from custody.services import (
    HolderStillHasMaterial,
    TransferNotReady,
    acknowledge_transfer,
    assert_can_deactivate,
    decline_transfer,
    escalate_overdue,
    holdings_of,
    mark_overdue,
    overdue_report,
)
from dispatch.models import GateOut, GateOutLine, GateOutPurpose
from dispatch.services import release_gate_out, submit_gate_out
from jobs.models import (
    CloseoutAction,
    Job,
    JobCloseout,
    JobCloseoutLine,
    JobStatus,
    Variance,
    VarianceStatus,
    VarianceType,
)
from jobs.reconciliation import reconcile_project, reconcile_site
from jobs.services import (
    CloseoutNotReady,
    JobHasUnaccountedMaterial,
    close_job,
    match_return,
    resolve_variance,
    submit_closeout,
)
from locations.factories import YardFactory
from locations.nodes import external_node, node_for_user
from network.factories import ClientFactory, ProjectFactory, SiteFactory
from receiving.models import GateIn, GateInLine, GateInSource
from stock.models import Condition, MovementType
from stock.services import MovementRequest, balance_at, post_movement


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def storekeeper(tenant):
    return UserFactory(organization=tenant, full_name="Sara Storekeeper")


@pytest.fixture
def technician(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def site(tenant):
    return SiteFactory(internal_ref="SLV-1001", name="Kileleshwa")


@pytest.fixture
def job(tenant, site, technician):
    return Job.objects.create(
        organization=tenant,
        reference="JOB-001",
        client=site.client,
        site=site,
        assignee=technician,
    )


def stock_in_tracked(tenant, node, item, quantity, *, serial_unit=None, reel=None):
    """Receive a serialized unit or a drum, which needs its own identity (§3.5)."""
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal(str(quantity)),
                from_node=external_node(tenant.pk),
                to_node=node,
                movement_type=MovementType.RECEIPT,
                tracking_mode=(
                    TrackingMode.SERIALIZED if serial_unit is not None else TrackingMode.REEL
                ),
                serial_unit=serial_unit,
                reel=reel,
            )
        )


def stock_in(tenant, node, item, quantity):
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal(str(quantity)),
                from_node=external_node(tenant.pk),
                to_node=node,
                movement_type=MovementType.RECEIPT,
            )
        )


def issue_to(tenant, yard, item, quantity, requester, holder, site):
    """Issue material through a real gate pass, as the yard would."""
    gate_out = GateOut.objects.create(
        organization=tenant,
        from_location=yard,
        site=site,
        custody_holder=holder,
        requested_by=requester,
        purpose_type=GateOutPurpose.INSTALLATION,
    )
    GateOutLine.objects.create(
        organization=tenant,
        gate_out=gate_out,
        item_type=item,
        tracking_mode=TrackingMode.BULK,
        requested_qty=Decimal(str(quantity)),
        uom=item.uom,
    )
    submit_gate_out(gate_out, submitted_by=requester)
    release_gate_out(gate_out, released_by=requester)
    return gate_out


def receive_return(tenant, yard, item, quantity, holder, storekeeper, site):
    """Receive material back the way the yard does it (H3).

    ``origin_site`` is what lets the site's reconciliation see the return at all
    (H4), and ``returned_by`` is what takes it off the holder's record (I1).
    """
    from receiving.services import post_gate_in

    gate_in = GateIn.objects.create(
        organization=tenant,
        source_type=GateInSource.RETURN_FROM_SITE,
        to_location=yard,
        received_at=timezone.now(),
        returned_by=holder,
        origin_site=site,
    )
    GateInLine.objects.create(
        organization=tenant,
        gate_in=gate_in,
        item_type=item,
        tracking_mode=TrackingMode.BULK,
        quantity=Decimal(str(quantity)),
        uom=item.uom,
        condition=Condition.USED_SERVICEABLE,
    )
    post_gate_in(gate_in, posted_by=storekeeper)
    return gate_in


def a_closeout(tenant, job, submitted_by, lines):
    """lines: [(action, item, quantity)]"""
    closeout = JobCloseout.objects.create(
        organization=tenant, job=job, submitted_by=submitted_by
    )
    for action, item, quantity in lines:
        JobCloseoutLine.objects.create(
            organization=tenant,
            closeout=closeout,
            action=action,
            item_type=item,
            quantity=Decimal(str(quantity)),
            uom=item.uom,
        )
    return closeout


class TestJob:
    """H1: accountability is explicit."""

    def test_a_job_names_who_must_close_it(self, tenant, job, technician):
        assert job.assignee == technician
        assert job.status == JobStatus.OPEN

    def test_a_project_is_optional(self, tenant, site, technician):
        """C7, D14: optional throughout."""
        job = Job.objects.create(
            organization=tenant,
            client=site.client,
            site=site,
            assignee=technician,
            project=None,
        )

        assert job.project_id is None

    def test_a_closed_job_records_when(self, tenant, job):
        from django.db import IntegrityError

        job.status = JobStatus.CLOSED
        with pytest.raises(IntegrityError), transaction.atomic():
            job.save()


class TestCloseoutPosting:
    """H2, §4.9: installed and consumed post now; returns become expectations."""

    def test_installed_material_moves_to_the_site(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        item = ItemTypeFactory(name="RRU")
        stock_in(tenant, yard.node, item, 10)
        issue_to(tenant, yard, item, 4, storekeeper, technician, site)

        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.INSTALLED, item, 3)])
        submit_closeout(closeout, submitted_by=technician)

        from locations.nodes import node_for_site

        assert balance_at(node_for_site(site), item) == Decimal("3")
        # And has left the technician's custody.
        assert balance_at(node_for_user(technician), item) == Decimal("1")

    def test_consumed_material_leaves_stock(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        item = ItemTypeFactory(name="Cable tie")
        stock_in(tenant, yard.node, item, 100)
        issue_to(tenant, yard, item, 50, storekeeper, technician, site)

        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.CONSUMED, item, 30)])
        submit_closeout(closeout, submitted_by=technician)

        from locations.nodes import consumed_node

        assert balance_at(consumed_node(tenant.pk), item) == Decimal("30")
        assert balance_at(node_for_user(technician), item) == Decimal("20")

    def test_metres_can_be_consumed_without_returning_the_drum(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """T5.2's stated criterion, and Q5's asymmetry.

        Q5: "partial installation of a serialized unit is impossible, but partial
        consumption of a drum on site is normal." So 120 m off a 500 m drum
        leaves 380 m on the same drum, still in the technician's custody — not a
        new drum, and not a returned one.
        """
        from stock.factories import ReelFactory

        item = ItemTypeFactory(
            name="Feeder cable", default_tracking_mode=TrackingMode.REEL, uom="m"
        )
        reel = ReelFactory(
            item_type=item,
            drum_number="D-CLOSEOUT-1",
            current_node=node_for_user(technician),
            initial_length=Decimal("500"),
            remaining_length=Decimal("500"),
        )
        stock_in_tracked(
            tenant, node_for_user(technician), item, 500, reel=reel
        )

        closeout = JobCloseout.objects.create(
            organization=tenant, job=job, submitted_by=technician
        )
        JobCloseoutLine.objects.create(
            organization=tenant,
            closeout=closeout,
            action=CloseoutAction.CONSUMED,
            item_type=item,
            reel=reel,
            quantity=Decimal("120"),
            uom=item.uom,
        )
        submit_closeout(closeout, submitted_by=technician)

        reel.refresh_from_db()
        assert reel.remaining_length == Decimal("380")
        # The drum itself has not gone anywhere.
        assert reel.current_node == node_for_user(technician)
        assert balance_at(node_for_user(technician), item) == Decimal("380")

    def test_installed_serials_appear_in_the_sites_installed_base(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """T5.3's stated criterion.

        The installed base is not a second table — it is the balance at the
        site's node (§3.1), which is why it cannot drift from the movements that
        put it there.
        """
        from locations.nodes import node_for_site
        from stock.factories import SerialUnitFactory
        from stock.queries import installed_base, installed_serials

        item = ItemTypeFactory(name="RRU 2600", default_tracking_mode=TrackingMode.SERIALIZED)
        unit = SerialUnitFactory(
            item_type=item,
            serial_number="RRU-INSTALL-1",
            current_node=node_for_user(technician),
        )
        stock_in_tracked(
            tenant, node_for_user(technician), item, 1, serial_unit=unit
        )

        closeout = JobCloseout.objects.create(
            organization=tenant, job=job, submitted_by=technician
        )
        JobCloseoutLine.objects.create(
            organization=tenant,
            closeout=closeout,
            action=CloseoutAction.INSTALLED,
            item_type=item,
            serial_unit=unit,
            quantity=Decimal("1"),
            uom=item.uom,
        )
        submit_closeout(closeout, submitted_by=technician)

        unit.refresh_from_db()
        assert unit.current_node == node_for_site(site)
        assert list(installed_serials(site=site)) == [unit]
        assert installed_base(site=site).filter(item_type=item).exists()
        # And it has left stock permanently: it is not in the yard, and not on
        # anybody's record.
        assert balance_at(node_for_user(technician), item) == Decimal("0")
        assert balance_at(yard.node, item) == Decimal("0")

    def test_a_declared_return_does_not_move_stock_yet(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """§4.9: recording it as received would be a lie the reconciliation hides.

        The material is still with the technician until the yard actually gets it.
        """
        item = ItemTypeFactory(name="Jumper")
        stock_in(tenant, yard.node, item, 20)
        issue_to(tenant, yard, item, 10, storekeeper, technician, site)

        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.RETURNING, item, 4)])
        submit_closeout(closeout, submitted_by=technician)

        # Still with the technician.
        assert balance_at(node_for_user(technician), item) == Decimal("10")
        # But now expected back.
        assert CustodyExpectation.objects.filter(item_type=item).exists()

    def test_a_recovery_creates_an_expectation_too(
        self, tenant, yard, job, storekeeper, technician
    ):
        """H2: recovered equipment creates an expected gate-in."""
        item = ItemTypeFactory(name="Old antenna")

        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.RECOVERED, item, 2)])
        submit_closeout(closeout, submitted_by=technician)

        assert CustodyExpectation.objects.filter(item_type=item).count() == 1

    def test_an_empty_closeout_cannot_be_submitted(self, tenant, job, technician):
        closeout = JobCloseout.objects.create(
            organization=tenant, job=job, submitted_by=technician
        )

        with pytest.raises(CloseoutNotReady, match="at least one line"):
            submit_closeout(closeout, submitted_by=technician)

    def test_a_closeout_cannot_be_submitted_twice(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """Otherwise every movement posts again (found while driving T5.10).

        A technician on a site double-taps, or the request times out and the
        phone retries. Posting a second time would install the same three
        antennas at the site again and take three more out of a custody that no
        longer has them — a corruption of the ledger caused by a flaky
        connection.
        """
        item = ItemTypeFactory(name="Twice-submitted RRU")
        stock_in(tenant, yard.node, item, 10)
        issue_to(tenant, yard, item, 4, storekeeper, technician, site)

        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.INSTALLED, item, 3)])
        submit_closeout(closeout, submitted_by=technician)

        from locations.nodes import node_for_site

        assert balance_at(node_for_site(site), item) == Decimal("3")

        with pytest.raises(CloseoutNotReady, match="already submitted"):
            submit_closeout(closeout, submitted_by=technician)

        # And nothing moved on the second attempt.
        assert balance_at(node_for_site(site), item) == Decimal("3")
        assert balance_at(node_for_user(technician), item) == Decimal("1")

    def test_submitting_moves_the_job_to_awaiting_closeout(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        issue_to(tenant, yard, item, 5, storekeeper, technician, site)

        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.INSTALLED, item, 5)])
        submit_closeout(closeout, submitted_by=technician)

        job.refresh_from_db()
        assert job.status == JobStatus.AWAITING_CLOSEOUT

    def test_a_storekeeper_may_close_out_on_a_technicians_behalf(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """Q2's mitigation, and why `on_behalf_of` exists.

        "If field staff will not use the app reliably, reconciliation quality
        collapses." Same endpoint, different actor — and the data then shows
        which is actually happening.
        """
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        issue_to(tenant, yard, item, 5, storekeeper, technician, site)

        closeout = JobCloseout.objects.create(
            organization=tenant, job=job, submitted_by=storekeeper, on_behalf_of=technician
        )
        JobCloseoutLine.objects.create(
            organization=tenant,
            closeout=closeout,
            action=CloseoutAction.INSTALLED,
            item_type=item,
            quantity=Decimal("5"),
            uom=item.uom,
        )
        submit_closeout(closeout, submitted_by=storekeeper)

        assert closeout.submitted_by == storekeeper
        assert closeout.on_behalf_of == technician

    def test_a_serialized_line_is_one_unit(self, tenant, job, technician):
        """Q5: partial installation of a serialized unit is impossible."""
        from stock.factories import SerialUnitFactory

        unit = SerialUnitFactory(current_node=node_for_user(technician))
        closeout = JobCloseout.objects.create(
            organization=tenant, job=job, submitted_by=technician
        )

        with pytest.raises(ValidationError, match="one unit per line"):
            JobCloseoutLine.objects.create(
                organization=tenant,
                closeout=closeout,
                action=CloseoutAction.INSTALLED,
                item_type=unit.item_type,
                serial_unit=unit,
                quantity=Decimal("2"),
                uom="ea",
            )


class TestReconciliation:
    """H4, T5.7, §14: the four figures must sum correctly."""

    def test_the_full_lifecycle_reconciles(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """§14's stated criterion, over a whole job.

        Issue 100, install 40, consume 25, return 35 — nothing unaccounted.
        """
        item = ItemTypeFactory(name="Feeder clamp")
        stock_in(tenant, yard.node, item, 200)
        issue_to(tenant, yard, item, 100, storekeeper, technician, site)

        closeout = a_closeout(
            tenant,
            job,
            technician,
            [
                (CloseoutAction.INSTALLED, item, 40),
                (CloseoutAction.CONSUMED, item, 25),
                (CloseoutAction.RETURNING, item, 35),
            ],
        )
        submit_closeout(closeout, submitted_by=technician)

        # The declared return physically comes back — through a real gate-in, so
        # this exercises the path the yard actually uses.
        receive_return(tenant, yard, item, 35, technician, storekeeper, site)

        result = reconcile_site(site)
        row = next(entry for entry in result["items"] if entry["item_type"] == "Feeder clamp")

        assert row["issued"] == Decimal("100")
        assert row["installed"] == Decimal("40")
        assert row["consumed"] == Decimal("25")
        assert row["returned"] == Decimal("35")
        assert row["unaccounted"] == Decimal("0")
        assert row["is_reconciled"] is True

    def test_material_still_out_shows_as_unaccounted(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """The figure an owner actually chases (H4)."""
        item = ItemTypeFactory(name="Torque wrench")
        stock_in(tenant, yard.node, item, 10)
        issue_to(tenant, yard, item, 6, storekeeper, technician, site)

        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.INSTALLED, item, 4)])
        submit_closeout(closeout, submitted_by=technician)

        result = reconcile_site(site)
        row = next(entry for entry in result["items"] if entry["item_type"] == "Torque wrench")

        assert row["issued"] == Decimal("6")
        assert row["installed"] == Decimal("4")
        assert row["unaccounted"] == Decimal("2")
        assert row["is_reconciled"] is False

    def test_unaccounted_is_derived_not_counted(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """Why the four figures can never disagree.

        "Unaccounted" is issued minus explained, never a separate tally — so
        there is no second number that could contradict the first three.
        """
        item = ItemTypeFactory(name="Connector")
        stock_in(tenant, yard.node, item, 50)
        issue_to(tenant, yard, item, 20, storekeeper, technician, site)
        closeout = a_closeout(
            tenant,
            job,
            technician,
            [(CloseoutAction.INSTALLED, item, 7), (CloseoutAction.CONSUMED, item, 3)],
        )
        submit_closeout(closeout, submitted_by=technician)

        row = next(
            entry
            for entry in reconcile_site(site)["items"]
            if entry["item_type"] == "Connector"
        )

        assert row["unaccounted"] == row["issued"] - (
            row["installed"] + row["consumed"] + row["returned"]
        )

    def test_a_transfer_inside_the_yard_is_not_an_issue(
        self, tenant, yard, storekeeper, site
    ):
        """Nothing left the yard's control, so nothing was issued."""
        from locations.factories import StoreFactory
        from stock.counting import transfer_stock

        item = ItemTypeFactory(name="Shelf stock")
        store = StoreFactory(parent=yard)
        stock_in(tenant, yard.node, item, 40)
        transfer_stock(
            organization=tenant,
            item_type=item,
            quantity=Decimal("15"),
            from_location=yard,
            to_location=store,
            performed_by=storekeeper,
        )

        result = reconcile_site(site)

        assert all(entry["issued"] == 0 for entry in result["items"])

    def test_a_project_reconciles_across_its_sites(
        self, tenant, yard, storekeeper, technician
    ):
        """H4: per project as well as per site."""
        client = ClientFactory(name="Safaricom")
        project = ProjectFactory(client=client, reference="WO-2001")
        first = SiteFactory(client=client, internal_ref="SLV-1")
        second = SiteFactory(client=client, internal_ref="SLV-2")
        project.sites.add(first, second)

        item = ItemTypeFactory(name="Antenna")
        stock_in(tenant, yard.node, item, 50)
        issue_to(tenant, yard, item, 6, storekeeper, technician, first)
        issue_to(tenant, yard, item, 4, storekeeper, technician, second)

        result = reconcile_project(project)

        assert result["totals"]["issued"] == Decimal("10")

    def test_closing_a_project_warns_rather_than_blocks(
        self, tenant, yard, storekeeper, technician
    ):
        """C7: "closing it warns if material remains unreconciled".

        Blocking is H5's rule and applies to jobs, not projects.
        """
        client = ClientFactory()
        project = ProjectFactory(client=client)
        site = SiteFactory(client=client)
        project.sites.add(site)

        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 20)
        issue_to(tenant, yard, item, 5, storekeeper, technician, site)

        summary = project.unreconciled_summary()

        assert summary["available"] is True
        assert summary["unreconciled"] is True


class TestReturnMatching:
    """H3, T5.4: a difference between declared and actual creates a variance."""

    def _declare_return(self, tenant, yard, job, storekeeper, technician, site, item, quantity):
        stock_in(tenant, yard.node, item, 100)
        issue_to(tenant, yard, item, quantity, storekeeper, technician, site)
        closeout = a_closeout(
            tenant, job, technician, [(CloseoutAction.RETURNING, item, quantity)]
        )
        submit_closeout(closeout, submitted_by=technician)
        return closeout

    def _receive(self, tenant, yard, item, quantity, storekeeper, returned_by=None):
        gate_in = GateIn.objects.create(
            organization=tenant,
            source_type=GateInSource.RETURN_FROM_SITE,
            to_location=yard,
            received_at=timezone.now(),
            returned_by=returned_by,
        )
        GateInLine.objects.create(
            organization=tenant,
            gate_in=gate_in,
            item_type=item,
            tracking_mode=TrackingMode.BULK,
            quantity=Decimal(str(quantity)),
            uom=item.uom,
            condition=Condition.USED_SERVICEABLE,
        )
        return gate_in

    def test_declaring_three_and_receiving_two_raises_one_variance(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """T5.4's stated criterion, exactly."""
        item = ItemTypeFactory(name="Jumper")
        self._declare_return(tenant, yard, job, storekeeper, technician, site, item, 3)
        gate_in = self._receive(tenant, yard, item, 2, storekeeper, returned_by=technician)

        variances = match_return(gate_in, matched_by=storekeeper)

        assert len(variances) == 1
        assert variances[0].expected == Decimal("3")
        assert variances[0].actual == Decimal("2")
        assert variances[0].type == VarianceType.RETURN

    def test_receiving_exactly_what_was_declared_raises_nothing(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        item = ItemTypeFactory(name="Jumper")
        self._declare_return(tenant, yard, job, storekeeper, technician, site, item, 3)
        gate_in = self._receive(tenant, yard, item, 3, storekeeper, returned_by=technician)

        assert match_return(gate_in, matched_by=storekeeper) == []

    def test_receiving_more_than_declared_is_also_a_variance(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """Something arrived that nobody said was coming.

        Just as interesting to an auditor as a shortfall, and easy to overlook if
        only shortfalls were checked.
        """
        item = ItemTypeFactory(name="Jumper")
        self._declare_return(tenant, yard, job, storekeeper, technician, site, item, 2)
        gate_in = self._receive(tenant, yard, item, 5, storekeeper, returned_by=technician)

        variances = match_return(gate_in, matched_by=storekeeper)

        assert len(variances) == 1
        assert variances[0].difference == Decimal("3")

    def test_a_matched_return_closes_its_expectation(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        item = ItemTypeFactory(name="Jumper")
        self._declare_return(tenant, yard, job, storekeeper, technician, site, item, 3)
        gate_in = self._receive(tenant, yard, item, 3, storekeeper, returned_by=technician)

        match_return(gate_in, matched_by=storekeeper)

        expectation = CustodyExpectation.objects.get(item_type=item)
        assert expectation.status == ExpectationStatus.RETURNED

    def test_a_partial_return_leaves_the_expectation_open(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        item = ItemTypeFactory(name="Jumper")
        self._declare_return(tenant, yard, job, storekeeper, technician, site, item, 5)
        gate_in = self._receive(tenant, yard, item, 2, storekeeper, returned_by=technician)

        match_return(gate_in, matched_by=storekeeper)

        expectation = CustodyExpectation.objects.get(item_type=item)
        assert expectation.status == ExpectationStatus.OPEN
        assert expectation.outstanding_quantity == Decimal("3")

    def test_a_purchase_is_not_matched_against_expectations(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """Only returns and recoveries are matched — a new delivery is not a return."""
        item = ItemTypeFactory(name="Jumper")
        self._declare_return(tenant, yard, job, storekeeper, technician, site, item, 3)

        purchase = GateIn.objects.create(
            organization=tenant,
            source_type=GateInSource.PURCHASE,
            supplier_name="Cable Supplies",
            to_location=yard,
            received_at=timezone.now(),
        )
        GateInLine.objects.create(
            organization=tenant,
            gate_in=purchase,
            item_type=item,
            tracking_mode=TrackingMode.BULK,
            quantity=Decimal("50"),
            uom=item.uom,
        )

        assert match_return(purchase, matched_by=storekeeper) == []


class TestVarianceResolution:
    """H3, M1: variances stay on the register until resolved."""

    def test_an_open_variance_is_listed(self, tenant):
        variance = Variance.objects.create(
            organization=tenant, type=VarianceType.RETURN, expected=3, actual=2
        )

        assert variance.is_open is True

    def test_resolving_requires_an_explanation(self, tenant, storekeeper):
        """"Resolved" with no explanation is worse than leaving it open."""
        variance = Variance.objects.create(
            organization=tenant, type=VarianceType.RETURN, expected=3, actual=2
        )

        with pytest.raises(CloseoutNotReady, match="explanation"):
            resolve_variance(variance, resolution="", resolved_by=storekeeper)

    def test_resolving_closes_it(self, tenant, storekeeper):
        variance = Variance.objects.create(
            organization=tenant, type=VarianceType.RETURN, expected=3, actual=2
        )

        resolve_variance(
            variance, resolution="Found in the vehicle", resolved_by=storekeeper
        )

        assert variance.status == VarianceStatus.RESOLVED
        assert variance.is_open is False

    def test_writing_off_is_distinct_from_resolving(self, tenant, storekeeper):
        """A write-off is a finding, not a fix — the register should say which."""
        variance = Variance.objects.create(
            organization=tenant, type=VarianceType.RETURN, expected=3, actual=2
        )

        resolve_variance(
            variance, resolution="Lost on site", resolved_by=storekeeper, write_off=True
        )

        assert variance.status == VarianceStatus.WRITTEN_OFF


class TestJobCloseGuard:
    """H5: blocked while material is unaccounted for, unless overridden."""

    def test_closing_is_blocked_while_material_is_outstanding(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 20)
        issue_to(tenant, yard, item, 5, storekeeper, technician, site)
        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.RETURNING, item, 5)])
        submit_closeout(closeout, submitted_by=technician)

        with pytest.raises(JobHasUnaccountedMaterial) as caught:
            close_job(job, closed_by=storekeeper)

        assert caught.value.details["outstanding"]
        job.refresh_from_db()
        assert job.status != JobStatus.CLOSED

    def test_an_override_needs_a_reason(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 20)
        issue_to(tenant, yard, item, 5, storekeeper, technician, site)
        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.RETURNING, item, 5)])
        submit_closeout(closeout, submitted_by=technician)

        with pytest.raises(JobHasUnaccountedMaterial, match="requires a reason"):
            close_job(job, closed_by=storekeeper, override=True, reason="")

    def test_an_override_with_a_reason_closes_and_is_flagged(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        """The record of a write-off is better than a job open forever."""
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 20)
        issue_to(tenant, yard, item, 5, storekeeper, technician, site)
        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.RETURNING, item, 5)])
        submit_closeout(closeout, submitted_by=technician)

        close_job(
            job,
            closed_by=storekeeper,
            override=True,
            reason="Written off — technician left the company",
        )

        assert job.status == JobStatus.CLOSED
        assert job.closed_with_variance is True
        assert "left the company" in job.close_reason

    def test_a_fully_reconciled_job_closes_without_an_override(
        self, tenant, yard, job, storekeeper, technician, site
    ):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 20)
        issue_to(tenant, yard, item, 5, storekeeper, technician, site)
        closeout = a_closeout(tenant, job, technician, [(CloseoutAction.INSTALLED, item, 5)])
        submit_closeout(closeout, submitted_by=technician)

        close_job(job, closed_by=storekeeper)

        assert job.status == JobStatus.CLOSED
        assert job.closed_with_variance is False


class TestCustodyView:
    """I1: a live view of what each technician holds."""

    def test_holdings_come_from_the_ledger(
        self, tenant, yard, storekeeper, technician, site
    ):
        """§4.10: no separate custody ledger, so the two can never disagree."""
        item = ItemTypeFactory(name="Multimeter")
        stock_in(tenant, yard.node, item, 5)
        issue_to(tenant, yard, item, 2, storekeeper, technician, site)

        holdings = holdings_of(technician)

        assert len(holdings) == 1
        assert holdings[0]["item"] == "Multimeter"
        assert holdings[0]["quantity"] == Decimal("2")

    def test_someone_holding_nothing_has_no_holdings(self, tenant, technician):
        assert holdings_of(technician) == []


class TestOverdueSweep:
    """I3, T5.8: escalating reminders that fire once per stage."""

    def _overdue_expectation(self, tenant, technician, days_late=1):
        # A distinct item each time: item type names are unique per tenant (C3),
        # and this helper is called more than once in a test.
        item = ItemTypeFactory(is_returnable=True, default_return_days=7)
        return CustodyExpectation.objects.create(
            organization=tenant,
            holder=technician,
            item_type=item,
            quantity=Decimal("1"),
            expected_return_date=timezone.now().date() - timedelta(days=days_late),
        )

    def test_a_past_due_expectation_is_flagged(self, tenant, technician):
        self._overdue_expectation(tenant, technician)

        assert mark_overdue(tenant.pk) == 1

        assert CustodyExpectation.objects.get().status == ExpectationStatus.OVERDUE

    def test_an_expectation_not_yet_due_is_not_flagged(self, tenant, technician):
        item = ItemTypeFactory(is_returnable=True, default_return_days=7)
        CustodyExpectation.objects.create(
            organization=tenant,
            holder=technician,
            item_type=item,
            quantity=Decimal("1"),
            expected_return_date=timezone.now().date() + timedelta(days=3),
        )

        assert mark_overdue(tenant.pk) == 0

    def test_the_holder_is_reminded_first(self, tenant, technician):
        """I3's chain starts with the person who has it."""
        self._overdue_expectation(tenant, technician, days_late=1)
        mark_overdue(tenant.pk)

        counts = escalate_overdue(tenant.pk)

        assert counts["holder"] == 1
        assert counts["supervisor"] == 0

    def test_each_stage_fires_once_not_once_per_run(self, tenant, technician):
        """T5.8's stated criterion.

        A nightly duplicate is how a control becomes noise, and then nobody reads
        any of them.
        """
        self._overdue_expectation(tenant, technician, days_late=1)
        mark_overdue(tenant.pk)

        first = escalate_overdue(tenant.pk)
        second = escalate_overdue(tenant.pk)

        assert first["holder"] == 1
        assert second["holder"] == 0

    def test_it_escalates_to_the_supervisor_then_the_owner(self, tenant, technician):
        """Q6's chain: holder -> storekeeper -> owner."""
        self._overdue_expectation(tenant, technician, days_late=10)
        mark_overdue(tenant.pk)

        assert escalate_overdue(tenant.pk)["holder"] == 1
        assert escalate_overdue(tenant.pk)["supervisor"] == 1
        assert escalate_overdue(tenant.pk)["owner"] == 1
        # And then nothing more.
        assert escalate_overdue(tenant.pk) == {"holder": 0, "supervisor": 0, "owner": 0}

    def test_the_report_groups_by_person_and_by_item(self, tenant, technician):
        """I4: "who keeps doing this?" and "what do we keep losing?"."""
        self._overdue_expectation(tenant, technician, days_late=2)
        self._overdue_expectation(tenant, technician, days_late=5)

        report = overdue_report(tenant.pk)

        assert report["total"] == 2
        assert len(report["by_person"]) == 1
        assert report["by_person"][0]["items"] == 2
        assert len(report["by_item"]) == 2


class TestCustodyHandover:
    """I5, T5.9: nothing moves until the receiver acknowledges."""

    def _pending_transfer(self, tenant, yard, storekeeper, technician, site, quantity=2):
        item = ItemTypeFactory(name="Fibre splicer")
        stock_in(tenant, yard.node, item, 10)
        issue_to(tenant, yard, item, quantity, storekeeper, technician, site)

        receiver = UserFactory(organization=tenant, full_name="Rita Receiver")
        transfer = CustodyTransfer.objects.create(
            organization=tenant,
            from_holder=technician,
            to_holder=receiver,
            requested_by=technician,
        )
        CustodyTransferLine.objects.create(
            organization=tenant,
            transfer=transfer,
            item_type=item,
            quantity=Decimal(str(quantity)),
            uom=item.uom,
        )
        return transfer, item, receiver

    def test_an_unacknowledged_transfer_leaves_custody_where_it_was(
        self, tenant, yard, storekeeper, technician, site
    ):
        """T5.9's stated criterion.

        A handover nobody confirmed is exactly how a tool goes missing while each
        person believes the other has it.
        """
        transfer, item, receiver = self._pending_transfer(
            tenant, yard, storekeeper, technician, site
        )

        assert transfer.status == TransferStatus.PENDING
        assert balance_at(node_for_user(technician), item) == Decimal("2")
        assert balance_at(node_for_user(receiver), item) == Decimal("0")

    def test_acknowledging_moves_the_material(
        self, tenant, yard, storekeeper, technician, site
    ):
        transfer, item, receiver = self._pending_transfer(
            tenant, yard, storekeeper, technician, site
        )

        acknowledge_transfer(transfer, actor=receiver)

        assert balance_at(node_for_user(technician), item) == Decimal("0")
        assert balance_at(node_for_user(receiver), item) == Decimal("2")
        assert transfer.status == TransferStatus.ACKNOWLEDGED
        assert transfer.number.startswith("CT-")

    def test_only_the_receiver_can_acknowledge(
        self, tenant, yard, storekeeper, technician, site
    ):
        """Otherwise the acknowledgement proves nothing."""
        transfer, _item, _receiver = self._pending_transfer(
            tenant, yard, storekeeper, technician, site
        )

        with pytest.raises(TransferNotReady, match="person receiving"):
            acknowledge_transfer(transfer, actor=technician)

    def test_declining_leaves_the_material_where_it_was(
        self, tenant, yard, storekeeper, technician, site
    ):
        transfer, item, receiver = self._pending_transfer(
            tenant, yard, storekeeper, technician, site
        )

        decline_transfer(transfer, actor=receiver, reason="Not my job today")

        assert transfer.status == TransferStatus.DECLINED
        assert balance_at(node_for_user(technician), item) == Decimal("2")

    def test_declining_requires_a_reason(
        self, tenant, yard, storekeeper, technician, site
    ):
        transfer, _item, receiver = self._pending_transfer(
            tenant, yard, storekeeper, technician, site
        )

        with pytest.raises(TransferNotReady, match="requires a reason"):
            decline_transfer(transfer, actor=receiver, reason="")

    def test_an_expectation_follows_the_new_holder(
        self, tenant, yard, storekeeper, technician, site
    ):
        """So the overdue chase goes to the right phone (I2, I3)."""
        transfer, item, receiver = self._pending_transfer(
            tenant, yard, storekeeper, technician, site
        )
        expectation = CustodyExpectation.objects.create(
            organization=tenant,
            holder=technician,
            item_type=item,
            quantity=Decimal("2"),
            expected_return_date=timezone.now().date(),
        )

        acknowledge_transfer(transfer, actor=receiver)

        expectation.refresh_from_db()
        assert expectation.holder == receiver

    def test_a_handover_needs_two_different_people(self, tenant, technician):
        with pytest.raises(ValidationError, match="two different people"):
            CustodyTransfer.objects.create(
                organization=tenant, from_holder=technician, to_holder=technician
            )


class TestDeactivatingAHolder:
    """B3's edge case, wired to real custody (T2.15's warning, T5.8)."""

    def test_someone_holding_material_cannot_be_deactivated(
        self, tenant, yard, storekeeper, technician, site
    ):
        """Deactivating them would orphan the material — still somewhere, but
        nobody accountable for it."""
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 5)
        issue_to(tenant, yard, item, 2, storekeeper, technician, site)

        with pytest.raises(HolderStillHasMaterial) as caught:
            assert_can_deactivate(technician)

        assert caught.value.details["holdings"]

    def test_someone_holding_nothing_can_be_deactivated(self, tenant, technician):
        assert_can_deactivate(technician)
