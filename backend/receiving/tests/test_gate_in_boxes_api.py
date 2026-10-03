"""T11.7 — boxes through the gate-in API and the offline replay (§4.15.5, §8)."""

from decimal import Decimal
from uuid import uuid4

from django.urls import reverse
from django.utils import timezone

from core.tenancy import tenant_context
from receiving.tests import test_receiving_api as base

auth = base.auth
signed_in = base.signed_in
fixtures = base.fixtures


def payload(yard, bulk, serialized, **extra):
    return {
        "source_type": "PURCHASE",
        "supplier_name": "Huawei Kenya",
        "to_location": yard.pk,
        "received_at": timezone.now().isoformat(),
        "boxes": [
            {"key": "pal", "code": "API-PAL-1", "parent_key": "", "label_text": "SSCC 1"},
            {"key": "ctn", "code": "", "parent_key": "pal", "label_text": ""},
            {"key": "bag", "code": "API-BAG-1", "parent_key": "pal", "label_text": ""},
        ],
        "lines": [
            {
                "item_type": serialized.pk,
                "tracking_mode": "SERIALIZED",
                "quantity": "2",
                "uom": "ea",
                "condition": "NEW",
                "serials": [
                    {"serial_number": "API-BX-001", "box_key": "ctn"},
                    {"serial_number": "API-BX-002", "box_key": "ctn"},
                ],
            },
            {
                "item_type": bulk.pk,
                "tracking_mode": "BULK",
                "quantity": "50",
                "uom": "ea",
                "condition": "NEW",
                "box_key": "bag",
            },
        ],
        **extra,
    }


def create(http, token, body):
    return http.post(
        reverse("v1:gate-in-list"), body, content_type="application/json", **auth(token)
    )


def post_it(http, token, gate_in_id):
    return http.post(
        reverse("v1:gate-in-post-document", args=[gate_in_id]),
        content_type="application/json",
        **auth(token),
    )


class TestGateInBoxesApi:
    def test_create_read_post_and_find(self, signed_in, fixtures):
        from stock.queries import find_by_identifier
        from stock.verification import verify_ledger

        http, token, organization, _owner = signed_in
        yard, bulk, serialized, _reel = fixtures

        created = create(http, token, payload(yard, bulk, serialized))
        assert created.status_code == 201, created.content
        body = created.json()
        assert [box["key"] for box in body["boxes"]] == ["pal", "ctn", "bag"]
        assert body["lines"][0]["serials"][0]["box_key"] == "ctn"
        assert body["lines"][1]["box_key"] == "bag"

        posted = post_it(http, token, body["id"])
        assert posted.status_code == 200, posted.content
        generated = posted.json()["boxes"][1]["code"]
        assert generated.startswith("BX-")

        with tenant_context(organization):
            unit = find_by_identifier("API-BX-001")["object"]
            assert unit.box.code == generated
            assert unit.box.parent.code == "API-PAL-1"
            assert verify_ledger(organization.pk).drifts == []

    def test_a_draft_keeps_its_boxes_when_edited(self, signed_in, fixtures):
        http, token, _organization, _owner = signed_in
        yard, bulk, serialized, _reel = fixtures
        body = create(http, token, payload(yard, bulk, serialized)).json()

        edited = http.patch(
            reverse("v1:gate-in-detail", args=[body["id"]]),
            {"notes": "counted twice"},
            content_type="application/json",
            **auth(token),
        )
        assert edited.status_code == 200
        assert len(edited.json()["boxes"]) == 3

        rewritten = http.patch(
            reverse("v1:gate-in-detail", args=[body["id"]]),
            {
                "boxes": [{"key": "pal", "code": "API-PAL-2"}],
                "lines": [
                    {
                        "item_type": bulk.pk,
                        "tracking_mode": "BULK",
                        "quantity": "5",
                        "uom": "ea",
                        "condition": "NEW",
                        "box_key": "pal",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        )
        assert rewritten.status_code == 200, rewritten.content
        assert [box["code"] for box in rewritten.json()["boxes"]] == ["API-PAL-2"]
        assert post_it(http, token, body["id"]).status_code == 200

    def test_a_refusal_comes_back_as_a_field_error(self, signed_in, fixtures):
        http, token, _organization, _owner = signed_in
        yard, bulk, serialized, _reel = fixtures
        body = payload(yard, bulk, serialized)
        body["boxes"].append({"key": "lonely", "code": "API-LONELY"})
        created = create(http, token, body).json()

        response = post_it(http, token, created["id"])

        assert response.status_code == 400
        errors = response.json()["error"]["field_errors"]
        assert "BOX_EMPTY" in errors["boxes.3.code"][0]

    def test_duplicate_keys_are_refused_on_save(self, signed_in, fixtures):
        http, token, _organization, _owner = signed_in
        yard, bulk, serialized, _reel = fixtures
        body = payload(yard, bulk, serialized)
        body["boxes"].append({"key": "pal", "code": "API-DUP"})

        assert create(http, token, body).status_code == 400

    def test_void_closes_every_box(self, signed_in, fixtures):
        from stock.models import Box, BoxStatus, SerialUnit
        from stock.verification import verify_ledger

        http, token, organization, _owner = signed_in
        yard, bulk, serialized, _reel = fixtures
        body = create(http, token, payload(yard, bulk, serialized)).json()
        post_it(http, token, body["id"])

        voided = http.post(
            reverse("v1:gate-in-void", args=[body["id"]]),
            {"reason": "Wrong pallet"},
            content_type="application/json",
            **auth(token),
        )

        assert voided.status_code == 200, voided.content
        with tenant_context(organization):
            assert set(Box.objects.values_list("status", flat=True)) == {BoxStatus.CLOSED}
            assert not SerialUnit.objects.filter(box__isnull=False).exists()
            assert verify_ledger(organization.pk).drifts == []


class TestOfflineReplay:
    def test_a_replayed_submission_posts_boxes_once(self, signed_in, fixtures):
        from receiving.models import GateIn
        from stock.models import Box, BoxBulkContent
        from stock.services import balance_at

        http, token, organization, _owner = signed_in
        yard, bulk, serialized, _reel = fixtures
        item = {
            "client_uuid": str(uuid4()),
            "operation": "GATE_IN",
            "payload": payload(yard, bulk, serialized),
            "captured_at": timezone.now().isoformat(),
        }

        def submit():
            return http.post(
                reverse("v1:sync-submissions"),
                {"submissions": [item]},
                content_type="application/json",
                **auth(token),
            )

        first = submit().json()
        again = submit().json()

        assert first["applied"] == 1, first
        assert again["replayed"] == 1
        with tenant_context(organization):
            assert GateIn.objects.count() == 1
            assert Box.objects.count() == 3
            assert BoxBulkContent.objects.get().quantity == Decimal("50")
            assert balance_at(yard.node, bulk) == Decimal("50")
