"""T12.7 — loose cable length beside drums (§7.3c; D10)."""

from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.urls import reverse

from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from locations.factories import YardFactory
from locations.nodes import consumed_node, external_node
from stock.factories import ReelFactory
from stock.models import MovementType, Reel, ReelStatus, StockBalance
from stock.services import (
    MovementRequest,
    OnDrumsOnly,
    balance_at,
    post_movement,
)
from stock.verification import verify_ledger


def move(**kwargs):
    with transaction.atomic():
        return post_movement(MovementRequest(**kwargs))


def build_yard(tenant):
    """1,000 m on two drums and 240 m loose, in one yard."""
    yard = YardFactory(name="Main yard")
    supplier = external_node(tenant.pk)
    item = ItemTypeFactory(name="Fibre cable", default_tracking_mode=TrackingMode.REEL, uom="m")
    drums = []
    for number in ("D-0007", "D-0009"):
        reel = ReelFactory(
            item_type=item,
            drum_number=number,
            current_node=supplier,
            initial_length=Decimal("500"),
            remaining_length=Decimal("500"),
        )
        move(
            item_type=item,
            quantity=Decimal("500"),
            from_node=supplier,
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=reel,
        )
        drums.append(reel)
    move(
        item_type=item,
        quantity=Decimal("240"),
        from_node=supplier,
        to_node=yard.node,
        movement_type=MovementType.RECEIPT,
        tracking_mode=TrackingMode.BULK,
    )
    return yard, item, drums


def take_loose(yard, item, quantity, movement_type=MovementType.ISSUE, to=None):
    return move(
        item_type=item,
        quantity=Decimal(str(quantity)),
        from_node=yard.node,
        to_node=to or consumed_node(item.organization_id),
        movement_type=movement_type,
        tracking_mode=TrackingMode.BULK,
    )


def clean(tenant):
    result = verify_ledger(tenant.pk)
    assert result.ok, [str(d) for d in result.drifts]


class TestLooseLength:
    def test_the_setup_is_clean(self, tenant):
        yard, item, _ = build_yard(tenant)
        assert balance_at(yard.node, item) == Decimal("1240")
        clean(tenant)

    def test_loose_length_can_be_taken(self, tenant):
        yard, item, drums = build_yard(tenant)
        take_loose(yard, item, 200)
        assert balance_at(yard.node, item) == Decimal("1040")
        for drum in drums:
            drum.refresh_from_db()
            assert drum.remaining_length == Decimal("500")
        clean(tenant)

    def test_taking_beyond_the_loose_length_names_the_drums(self, tenant):
        yard, item, _ = build_yard(tenant)
        take_loose(yard, item, 200)

        with pytest.raises(OnDrumsOnly) as raised:
            take_loose(yard, item, 100)

        assert raised.value.code == "ON_DRUMS_ONLY"
        assert raised.value.status_code == 409
        assert str(raised.value) == (
            "Only 40 m is loose here; the rest is on drums D-0007 (500 m) and "
            "D-0009 (500 m). Take it from a drum."
        )
        assert raised.value.details == {"drums": ["D-0007", "D-0009"], "loose": "40.000"}
        assert balance_at(yard.node, item) == Decimal("1040")
        clean(tenant)

    def test_the_last_of_the_loose_length_can_go(self, tenant):
        yard, item, _ = build_yard(tenant)
        take_loose(yard, item, 240)
        assert balance_at(yard.node, item) == Decimal("1000")
        clean(tenant)

    def test_a_drum_still_issues_and_closes_at_zero(self, tenant):
        yard, item, drums = build_yard(tenant)
        move(
            item_type=item,
            quantity=Decimal("500"),
            from_node=yard.node,
            to_node=consumed_node(tenant.pk),
            movement_type=MovementType.ISSUE,
            tracking_mode=TrackingMode.REEL,
            reel=drums[0],
        )
        drums[0].refresh_from_db()
        assert drums[0].remaining_length == 0
        assert drums[0].status == ReelStatus.CLOSED
        # The drum is gone from the sum, so loose is still 240.
        take_loose(yard, item, 240)
        with pytest.raises(OnDrumsOnly) as raised:
            take_loose(yard, item, 1)
        assert "drum D-0009 (500 m)" in str(raised.value)
        clean(tenant)

    def test_a_correction_may_go_beyond_loose(self, tenant):
        yard, item, _ = build_yard(tenant)
        take_loose(yard, item, 300, movement_type=MovementType.ADJUST)
        assert balance_at(yard.node, item) == Decimal("940")
        # Drums now hold more than the balance: the verifier says so.
        result = verify_ledger(tenant.pk)
        assert [d.kind for d in result.drifts] == ["negative loose length"]

    def test_other_items_are_not_checked(self, tenant, django_assert_num_queries):
        yard = YardFactory(name="Main yard")
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.BULK)
        move(
            item_type=item,
            quantity=Decimal("10"),
            from_node=external_node(tenant.pk),
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
        )
        take_loose(yard, item, 10)
        assert balance_at(yard.node, item) == 0

    def test_a_reel_item_with_no_drums_is_all_loose(self, tenant):
        yard = YardFactory(name="Main yard")
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.REEL, uom="m")
        move(
            item_type=item,
            quantity=Decimal("50"),
            from_node=external_node(tenant.pk),
            to_node=yard.node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.BULK,
        )
        take_loose(yard, item, 50)
        clean(tenant)


class TestVerification:
    def test_a_drum_longer_than_the_balance_is_drift(self, tenant):
        _yard, _item, drums = build_yard(tenant)
        clean(tenant)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE stock_reel SET initial_length = 2000, remaining_length = 1500 "
                "WHERE id = %s",
                [drums[0].pk],
            )
        result = verify_ledger(tenant.pk)
        kinds = {d.kind for d in result.drifts}
        assert "negative loose length" in kinds


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
    return client, token, result["organization"]


class TestStockApi:
    def test_a_reel_row_carries_on_drums_and_loose(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization):
            yard, item, _ = build_yard(organization)
            take_loose(yard, item, 200)
            plain = ItemTypeFactory(name="Jumper", default_tracking_mode=TrackingMode.BULK)
            move(
                item_type=plain,
                quantity=Decimal("5"),
                from_node=external_node(organization.pk),
                to_node=yard.node,
                movement_type=MovementType.RECEIPT,
            )
            item_id, plain_id = item.pk, plain.pk

        response = http.get(
            reverse("v1:stock-on-hand"), HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        assert response.status_code == 200
        rows = {row["item_type"]: row for row in response.json()["results"]}
        reel_row = rows[item_id]
        assert Decimal(reel_row["quantity"]) == 1040
        assert Decimal(reel_row["on_drums"]) == 1000
        assert Decimal(reel_row["loose"]) == 40
        assert rows[plain_id]["on_drums"] is None
        assert rows[plain_id]["loose"] is None

    def test_the_split_costs_no_query_per_row(self, signed_in, django_assert_max_num_queries):
        http, token, organization = signed_in
        with tenant_context(organization):
            for _ in range(4):
                build_yard_reel_only(organization)
        with django_assert_max_num_queries(25):
            response = http.get(
                reverse("v1:stock-on-hand"), HTTP_AUTHORIZATION=f"Bearer {token}"
            )
        assert len(response.json()["results"]) == 4


def build_yard_reel_only(tenant):
    yard = YardFactory()
    item = ItemTypeFactory(default_tracking_mode=TrackingMode.REEL, uom="m")
    reel = ReelFactory(item_type=item, current_node=external_node(tenant.pk))
    move(
        item_type=item,
        quantity=reel.remaining_length,
        from_node=external_node(tenant.pk),
        to_node=yard.node,
        movement_type=MovementType.RECEIPT,
        tracking_mode=TrackingMode.REEL,
        reel=reel,
    )
    assert Reel.objects.filter(pk=reel.pk).exists() and StockBalance.objects.exists()
