"""T13.4 - gate-out diversions (design 4.16.5; Q3).

To the site an item is earmarked for, it goes without a word and release writes
DELIVERED. To anywhere else it needs a reason at submit, the approver sees it,
and release writes DIVERTED with that reason.
"""

from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals.models import ApprovalRequest
from approvals.views import ApprovalRequestSerializer
from catalogue.factories import ItemCategoryFactory, ItemTypeFactory
from catalogue.models import Criticality, TrackingMode
from core.tenancy import tenant_context
from dispatch.diversions import destination_sites, pass_diversions
from dispatch.models import GateOutLineReel, GateOutLineSerial, GateOutPurpose
from dispatch.services import (
    GateOutNotReady,
    approve_gate_out,
    release_gate_out,
    submit_gate_out,
)
from dispatch.tests.test_dispatch_api import auth, signed_in  # noqa: F401
from dispatch.tests.test_gate_out import add_line, make_gate_out, rule_for, stock_in
from dispatch.views import GateOutSerializer
from jobs.models import Job
from locations.factories import StoreFactory, YardFactory
from locations.nodes import external_node
from network.factories import ProjectFactory, SiteFactory
from stock.factories import ReelFactory, SerialUnitFactory
from stock.models import (
    BulkEarmark,
    Condition,
    EarmarkAction,
    EarmarkEvent,
    MovementType,
)
from stock.services import MovementRequest, post_movement
from stock.verification import verify_ledger


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def requester(tenant):
    return UserFactory(organization=tenant, full_name="Sara Storekeeper")


@pytest.fixture
def holder(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def alpha(tenant):
    return SiteFactory(name="Site Alpha")


@pytest.fixture
def bravo(tenant):
    return SiteFactory(name="Site Bravo")


@pytest.fixture
def charlie(tenant):
    return SiteFactory(name="Site Charlie")


def ok(tenant):
    result = verify_ledger(tenant.pk)
    assert result.ok, [str(d) for d in result.drifts]


def serial_item():
    return ItemTypeFactory(name="RRU 5526w", default_tracking_mode=TrackingMode.SERIALIZED)


def receive_units(tenant, node, item, site, count, prefix="RRU"):
    units = []
    for n in range(count):
        unit = SerialUnitFactory(
            item_type=item,
            serial_number=f"{prefix}-{n + 1}",
            current_node=external_node(tenant.pk),
        )
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("1"),
                    from_node=external_node(tenant.pk),
                    to_node=node,
                    movement_type=MovementType.RECEIPT,
                    tracking_mode=TrackingMode.SERIALIZED,
                    serial_unit=unit,
                )
            )
        unit.refresh_from_db()
        unit.earmark_site = site
        unit.save()
        units.append(unit)
    return units


def serial_line(gate_out, item, units, **kwargs):
    line = add_line(gate_out, item, len(units), tracking_mode=TrackingMode.SERIALIZED, **kwargs)
    for unit in units:
        GateOutLineSerial.objects.create(
            organization=gate_out.organization, line=line, serial_unit=unit
        )
    return line


def earmark(site, node, item, quantity):
    return BulkEarmark.objects.create(
        site=site,
        node=node,
        item_type=item,
        condition=Condition.NEW,
        quantity=Decimal(str(quantity)),
    )


def events(action):
    return list(EarmarkEvent.objects.filter(action=action).order_by("id"))


class TestDestinationSites:
    def test_site_job_project_and_none(self, tenant, yard, requester, holder, alpha, bravo):
        project = ProjectFactory(reference="WO-1", manager=requester)
        for site in (alpha, bravo):
            Job.objects.create(
                organization=tenant,
                reference=f"J-{site.pk}",
                client=site.client,
                site=site,
                project=project,
                assignee=holder,
            )
        job = Job.objects.filter(site=alpha).get()

        assert destination_sites(make_gate_out(tenant, yard, requester, holder, site=alpha)) == {
            alpha
        }
        assert destination_sites(
            make_gate_out(tenant, yard, requester, holder, project=project)
        ) == {alpha, bravo}
        by_job = make_gate_out(tenant, yard, requester, holder, site=alpha, job=job)
        assert destination_sites(by_job) == {alpha}
        assert destination_sites(
            make_gate_out(tenant, yard, requester, holder, client=alpha.client,
                          purpose_type=GateOutPurpose.RETURN_TO_CLIENT)
        ) == frozenset()


class TestUnits:
    def test_to_the_earmarked_site_needs_no_reason_and_delivers(
        self, tenant, yard, requester, holder, alpha
    ):
        item = serial_item()
        units = receive_units(tenant, yard.node, item, alpha, 2)
        gate_out = make_gate_out(tenant, yard, requester, holder, site=alpha)
        serial_line(gate_out, item, units)

        submit_gate_out(gate_out, submitted_by=requester)
        release_gate_out(gate_out, released_by=requester)

        delivered = events(EarmarkAction.DELIVERED)
        assert len(delivered) == 2
        assert {e.site for e in delivered} == {alpha}
        assert {e.document_type for e in delivered} == {"dispatch.GateOut"}
        assert {e.document_number for e in delivered} == {gate_out.number}
        ok(tenant)

    def test_to_another_site_needs_a_reason_naming_the_site(
        self, tenant, yard, requester, holder, alpha, bravo
    ):
        item = serial_item()
        units = receive_units(tenant, yard.node, item, alpha, 2)
        gate_out = make_gate_out(tenant, yard, requester, holder, site=bravo)
        serial_line(gate_out, item, units)

        with pytest.raises(GateOutNotReady) as refused:
            submit_gate_out(gate_out, submitted_by=requester)

        message = refused.value.field_errors["lines.0.divert_reason"][0]
        assert message == (
            "2 RRU 5526w are earmarked for Site Alpha. "
            "Say why they are going to Site Bravo, or take free stock."
        )

    def test_sent_with_a_reason_it_releases_as_diverted(
        self, tenant, yard, requester, holder, alpha, bravo
    ):
        item = serial_item()
        units = receive_units(tenant, yard.node, item, alpha, 2)
        gate_out = make_gate_out(tenant, yard, requester, holder, site=bravo)
        serial_line(gate_out, item, units, divert_reason="Alpha was cancelled")

        submit_gate_out(gate_out, submitted_by=requester)
        release_gate_out(gate_out, released_by=requester)

        diverted = events(EarmarkAction.DIVERTED)
        assert len(diverted) == 2
        assert {(e.site, e.to_site, e.reason) for e in diverted} == {
            (alpha, bravo, "Alpha was cancelled")
        }
        assert {e.document_number for e in diverted} == {gate_out.number}
        ok(tenant)

    def test_a_person_with_no_job_using_earmarked_units_needs_a_reason(
        self, tenant, yard, requester, holder, alpha
    ):
        item = serial_item()
        units = receive_units(tenant, yard.node, item, alpha, 1)
        gate_out = make_gate_out(
            tenant, yard, requester, holder, client=alpha.client,
            purpose_type=GateOutPurpose.RETURN_TO_CLIENT,
        )
        serial_line(gate_out, item, units)

        with pytest.raises(GateOutNotReady) as refused:
            submit_gate_out(gate_out, submitted_by=requester)

        message = refused.value.field_errors["lines.0.divert_reason"][0]
        assert message.startswith("1 RRU 5526w is earmarked for Site Alpha.")

    def test_a_project_pass_counts_the_projects_job_sites_as_own(
        self, tenant, yard, requester, holder, alpha, bravo
    ):
        project = ProjectFactory(reference="WO-2", manager=requester)
        for site in (alpha, bravo):
            Job.objects.create(
                organization=tenant,
                reference=f"J-{site.pk}",
                client=site.client,
                site=site,
                project=project,
                assignee=holder,
            )
        item = serial_item()
        for_alpha = receive_units(tenant, yard.node, item, alpha, 1, prefix="A")
        for_bravo = receive_units(tenant, yard.node, item, bravo, 1, prefix="B")
        gate_out = make_gate_out(tenant, yard, requester, holder, project=project)
        serial_line(gate_out, item, for_alpha + for_bravo)

        submit_gate_out(gate_out, submitted_by=requester)
        if not gate_out.is_releasable:  # a project pass routes to its manager
            approve_gate_out(gate_out, actor=project.manager)
        release_gate_out(gate_out, released_by=requester)

        assert len(events(EarmarkAction.DELIVERED)) == 2
        assert not events(EarmarkAction.DIVERTED)
        ok(tenant)

    def test_a_pass_to_another_location_carries_the_earmark(
        self, tenant, yard, requester, holder, alpha
    ):
        store = StoreFactory(name="Store B", parent=yard)
        item = serial_item()
        units = receive_units(tenant, yard.node, item, alpha, 1)
        gate_out = make_gate_out(tenant, yard, requester, holder, to_location=store)
        serial_line(gate_out, item, units)

        submit_gate_out(gate_out, submitted_by=requester)
        release_gate_out(gate_out, released_by=requester)

        assert len(events(EarmarkAction.MOVED)) == 1
        assert not events(EarmarkAction.DIVERTED) and not events(EarmarkAction.DELIVERED)
        units[0].refresh_from_db()
        assert units[0].earmark_site == alpha
        ok(tenant)

    def test_a_drum_for_another_site_needs_a_reason(
        self, tenant, yard, requester, holder, alpha, bravo
    ):
        item = ItemTypeFactory(name="Fibre", default_tracking_mode=TrackingMode.REEL, uom="m")
        reel = ReelFactory(
            item_type=item,
            drum_number="D-9",
            current_node=external_node(tenant.pk),
            initial_length=Decimal("100"),
            remaining_length=Decimal("100"),
        )
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("100"),
                    from_node=external_node(tenant.pk),
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                    tracking_mode=TrackingMode.REEL,
                    reel=reel,
                )
            )
        reel.refresh_from_db()
        reel.earmark_site = alpha
        reel.save()
        gate_out = make_gate_out(tenant, yard, requester, holder, site=bravo)
        line = add_line(gate_out, item, 100, tracking_mode=TrackingMode.REEL)
        GateOutLineReel.objects.create(
            organization=tenant, line=line, reel=reel, length_requested=Decimal("100")
        )

        with pytest.raises(GateOutNotReady) as refused:
            submit_gate_out(gate_out, submitted_by=requester)
        assert "Drum D-9 of Fibre is earmarked for Site Alpha" in (
            refused.value.field_errors["lines.0.divert_reason"][0]
        )
        assert pass_diversions(gate_out)[line.pk][0]["drums"] == ["D-9"]


class TestBulk:
    @pytest.fixture
    def item(self, tenant):
        return ItemTypeFactory(name="Jumper", default_tracking_mode=TrackingMode.BULK, uom="ea")

    def test_own_earmark_plus_free_is_no_diversion(
        self, tenant, yard, requester, holder, alpha, bravo, item
    ):
        stock_in(tenant, yard.node, item, 30)
        earmark(alpha, yard.node, item, 10)
        earmark(bravo, yard.node, item, 5)  # 15 free
        gate_out = make_gate_out(tenant, yard, requester, holder, site=alpha)
        add_line(gate_out, item, 25)  # 10 own + 15 free

        submit_gate_out(gate_out, submitted_by=requester)
        release_gate_out(gate_out, released_by=requester)

        assert [e.quantity for e in events(EarmarkAction.DELIVERED)] == [Decimal("10")]
        assert not events(EarmarkAction.DIVERTED)
        ok(tenant)

    def test_dipping_into_another_sites_earmark_needs_a_reason(
        self, tenant, yard, requester, holder, alpha, bravo, item
    ):
        stock_in(tenant, yard.node, item, 30)
        earmark(alpha, yard.node, item, 10)
        earmark(bravo, yard.node, item, 5)
        gate_out = make_gate_out(tenant, yard, requester, holder, site=alpha)
        line = add_line(gate_out, item, 28)  # 10 own + 15 free + 3 of Bravo's

        with pytest.raises(GateOutNotReady) as refused:
            submit_gate_out(gate_out, submitted_by=requester)
        assert refused.value.field_errors["lines.0.divert_reason"] == [
            "3 ea of Jumper are earmarked for Site Bravo. "
            "Say why they are going to Site Alpha, or take free stock."
        ]

        line.divert_reason = "Urgent"
        line.save()
        submit_gate_out(gate_out, submitted_by=requester)
        release_gate_out(gate_out, released_by=requester)

        diverted = events(EarmarkAction.DIVERTED)
        assert [(e.site, e.quantity, e.reason, e.to_site) for e in diverted] == [
            (bravo, Decimal("3"), "Urgent", alpha)
        ]
        ok(tenant)


class TestPayloads:
    def diverting_pass(self, tenant, yard, requester, holder, alpha, bravo, **line_kwargs):
        item = serial_item()
        units = receive_units(tenant, yard.node, item, alpha, 2)
        gate_out = make_gate_out(tenant, yard, requester, holder, site=bravo)
        serial_line(gate_out, item, units, divert_reason="Alpha is on hold", **line_kwargs)
        return gate_out, item

    def test_the_read_side_carries_diversions_and_the_reason(
        self, tenant, yard, requester, holder, alpha, bravo
    ):
        gate_out, _item = self.diverting_pass(tenant, yard, requester, holder, alpha, bravo)
        submit_gate_out(gate_out, submitted_by=requester)
        gate_out.refresh_from_db()

        line = GateOutSerializer(gate_out).data["lines"][0]

        assert line["divert_reason"] == "Alpha is on hold"
        assert line["diversions"] == [
            {
                "site": alpha.pk,
                "site_name": "Site Alpha",
                "quantity": "2",
                "serials": ["RRU-1", "RRU-2"],
            }
        ]

    def test_the_read_side_costs_a_bounded_number_of_queries(
        self, tenant, yard, requester, holder, alpha, bravo
    ):
        gate_out, _item = self.diverting_pass(tenant, yard, requester, holder, alpha, bravo)
        bulk = ItemTypeFactory(name="Jumper", default_tracking_mode=TrackingMode.BULK, uom="ea")
        stock_in(tenant, yard.node, bulk, 10)
        earmark(alpha, yard.node, bulk, 10)
        add_line(gate_out, bulk, 4)
        extra = ItemTypeFactory(name="Clamp", default_tracking_mode=TrackingMode.BULK, uom="ea")
        add_line(gate_out, extra, 1)
        lines = list(
            gate_out.lines.select_related("item_type")
            .prefetch_related("serials__serial_unit", "reels__reel")
        )
        with CaptureQueriesContext(connection) as queries:
            found = pass_diversions(gate_out, lines)
        assert [bool(v) for v in found.values()] == [True, True, False]
        assert len(queries) <= 8

    def test_the_approver_sees_diversions_and_the_reason(
        self, tenant, yard, requester, holder, alpha, bravo
    ):
        role = RoleFactory(name="Owner", codenames=[PERM.GATE_OUT_APPROVE])
        UserRoleFactory(user=UserFactory(organization=tenant), role=role)
        rule_for(Criticality.HIGH, role)
        item = ItemTypeFactory(
            name="RRU 5526w",
            default_tracking_mode=TrackingMode.SERIALIZED,
            category=ItemCategoryFactory(criticality=Criticality.HIGH),
        )
        units = receive_units(tenant, yard.node, item, alpha, 2)
        gate_out = make_gate_out(tenant, yard, requester, holder, site=bravo)
        serial_line(gate_out, item, units, divert_reason="Alpha is on hold")
        submit_gate_out(gate_out, submitted_by=requester)

        request = ApprovalRequest.objects.get(document_id=str(gate_out.pk))
        document = ApprovalRequestSerializer().get_document(request)

        line = document["lines"][0]
        assert line["divert_reason"] == "Alpha is on hold"
        assert line["diversions"][0]["site_name"] == "Site Alpha"
        assert line["diversions"][0]["serials"] == ["RRU-1", "RRU-2"]

    def test_nothing_earmarked_means_no_diversions(
        self, tenant, yard, requester, holder, bravo
    ):
        item = ItemTypeFactory(name="Clamp", default_tracking_mode=TrackingMode.BULK, uom="ea")
        stock_in(tenant, yard.node, item, 5)
        gate_out = make_gate_out(tenant, yard, requester, holder, site=bravo)
        add_line(gate_out, item, 2)
        submit_gate_out(gate_out, submitted_by=requester)
        assert GateOutSerializer(gate_out).data["lines"][0]["diversions"] == []


class TestReleasableBundle:
    def test_the_bundle_carries_divert_reason(self, signed_in):  # noqa: F811
        http, token, organization, owner = signed_in
        with tenant_context(organization):
            yard = YardFactory(name="Bundle yard")
            bravo = SiteFactory(name="Site Bravo")
            item = ItemTypeFactory(name="Clamp", default_tracking_mode=TrackingMode.BULK, uom="ea")
            stock_in(organization, yard.node, item, 5)
            alpha = SiteFactory(name="Site Alpha")
            earmark(alpha, yard.node, item, 5)
            gate_out = make_gate_out(organization, yard, owner, owner, site=bravo)
            add_line(gate_out, item, 2, divert_reason="Alpha slipped")
            submit_gate_out(gate_out, submitted_by=owner)
            gate_out.refresh_from_db()
            assert gate_out.is_releasable

        body = http.get(reverse("v1:sync-bundle"), **auth(token)).json()

        row = next(r for r in body["releasable_gate_outs"] if r["id"] == gate_out.pk)
        assert row["lines"][0]["divert_reason"] == "Alpha slipped"
