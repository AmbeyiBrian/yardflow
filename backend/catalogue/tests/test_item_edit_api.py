"""C10 / T12.5: editing an item, and the lock on tracking mode and unit."""

from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from catalogue.factories import ItemCategoryFactory, ItemTypeFactory
from catalogue.models import TrackingMode
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from locations.factories import YardFactory
from locations.nodes import external_node
from stock.models import MovementType
from stock.services import MovementRequest, post_movement


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


def receive(item, organization):
    with tenant_context(organization):
        yard = YardFactory(name="Main yard")
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("5"),
                    from_node=external_node(organization.pk),
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                )
            )


@pytest.fixture
def unmoved(signed_in):
    _h, _t, organization, _o = signed_in
    with tenant_context(organization):
        return ItemTypeFactory(name="Test BBU", default_tracking_mode=TrackingMode.BULK, uom="ea")


@pytest.fixture
def moved(signed_in):
    _h, _t, organization, _o = signed_in
    with tenant_context(organization):
        item = ItemTypeFactory(name="Test BBU", default_tracking_mode=TrackingMode.BULK, uom="ea")
    receive(item, organization)
    return item


def patch(signed_in, item, body):
    http, token, *_ = signed_in
    return http.patch(
        reverse("v1:item-type-detail", args=[item.pk]),
        body,
        content_type="application/json",
        **auth(token),
    )


class TestUnmovedItem:
    def test_tracking_mode_and_unit_can_change(self, signed_in, unmoved):
        response = patch(signed_in, unmoved, {"default_tracking_mode": "REEL", "uom": "m"})
        assert response.status_code == 200, response.content
        body = response.json()
        assert (body["default_tracking_mode"], body["uom"]) == ("REEL", "m")
        assert body["tracking_locked"] is False


class TestMovedItem:
    def test_a_changed_tracking_mode_is_refused_naming_the_field(self, signed_in, moved):
        response = patch(signed_in, moved, {"default_tracking_mode": "SERIALIZED"})
        assert response.status_code == 409, response.content
        text = response.content.decode()
        assert "ITEM_TRACKING_LOCKED" in text
        assert "Test BBU has stock history, so its tracking mode cannot change" in text
        assert "default_tracking_mode" in text
        moved.refresh_from_db()
        assert moved.default_tracking_mode == TrackingMode.BULK

    def test_a_changed_unit_is_refused_naming_the_field(self, signed_in, moved):
        response = patch(signed_in, moved, {"uom": "kg"})
        assert response.status_code == 409, response.content
        text = response.content.decode()
        assert "ITEM_TRACKING_LOCKED" in text
        assert "its unit of measure cannot change" in text
        moved.refresh_from_db()
        assert moved.uom == "ea"

    def test_a_put_that_changes_either_is_refused_too(self, signed_in, moved):
        http, token, *_ = signed_in
        response = http.put(
            reverse("v1:item-type-detail", args=[moved.pk]),
            {"category": moved.category_id, "name": "Test BBU", "uom": "kg"},
            content_type="application/json",
            **auth(token),
        )
        assert response.status_code == 409, response.content

    def test_sending_the_same_values_is_fine(self, signed_in, moved):
        response = patch(
            signed_in, moved, {"default_tracking_mode": "BULK", "uom": "ea", "name": "Test BBU 2"}
        )
        assert response.status_code == 200, response.content
        assert response.json()["tracking_locked"] is True

    def test_every_other_field_still_changes(self, signed_in, moved):
        _h, _t, organization, _o = signed_in
        with tenant_context(organization):
            category = ItemCategoryFactory(name="Radio", criticality="HIGH")
        body = {
            "name": "BBU 5216",
            "code": "BBU-5216",
            "category": category.pk,
            "description": "Baseband unit",
            "is_returnable": True,
            "default_return_days": 30,
            "min_stock_qty": "4",
        }
        response = patch(signed_in, moved, body)
        assert response.status_code == 200, response.content
        moved.refresh_from_db()
        assert moved.name == "BBU 5216"
        assert moved.code == "BBU-5216"
        assert moved.category_id == category.pk
        assert moved.description == "Baseband unit"
        assert moved.is_returnable is True
        assert moved.default_return_days == 30
        assert moved.min_stock_qty == Decimal("4")
        # Criticality is inherited from the category, so it follows the move.
        assert response.json()["criticality"] == "HIGH"


class TestTrackingLockedFlag:
    def test_flag_on_list_and_detail(self, signed_in, moved):
        http, token, organization, _o = signed_in
        with tenant_context(organization):
            fresh = ItemTypeFactory(name="Fresh item")
        rows = http.get(reverse("v1:item-type-list"), **auth(token)).json()["results"]
        flags = {row["name"]: row["tracking_locked"] for row in rows}
        assert flags["Test BBU"] is True
        assert flags["Fresh item"] is False
        detail = http.get(reverse("v1:item-type-detail", args=[moved.pk]), **auth(token))
        assert detail.json()["tracking_locked"] is True
        detail = http.get(reverse("v1:item-type-detail", args=[fresh.pk]), **auth(token))
        assert detail.json()["tracking_locked"] is False

    def test_list_queries_do_not_grow_per_item(self, signed_in):
        http, token, organization, _o = signed_in

        def count(n):
            with tenant_context(organization):
                for i in range(n):
                    ItemTypeFactory(name=f"Extra {n}-{i}")
            with CaptureQueriesContext(connection) as ctx:
                response = http.get(reverse("v1:item-type-list"), **auth(token))
            assert response.status_code == 200
            return len(ctx)

        few = count(2)
        many = count(8)
        assert many <= few
