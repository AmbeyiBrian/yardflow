"""T11.8 — the box API and box lookup (§4.15.9; P4, P6, P7, P8)."""

from decimal import Decimal

import pytest
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

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


def login_as(client, email):
    return client.post(
        reverse("v1:auth:login"),
        {"identifier": email, "password": "a good long password"},
        content_type="application/json",
    ).json()["access"]


def post(**kwargs):
    from stock.services import MovementRequest, post_movement

    with transaction.atomic():
        return post_movement(MovementRequest(**kwargs))


@pytest.fixture
def world(signed_in):
    """A yard and a store, a pallet PAL-1 holding carton CTN-1 (two units and
    a bulk claim) and one loose unit in the pallet itself."""
    _http, _token, organization, owner = signed_in
    with tenant_context(organization):
        from catalogue.factories import ItemTypeFactory
        from catalogue.models import TrackingMode
        from locations.factories import StoreFactory, YardFactory
        from locations.nodes import external_node, node_for_location
        from receiving.models import GateIn
        from stock.box_hooks import EventContext
        from stock.boxes import create_box, put_bulk, put_units
        from stock.factories import SerialUnitFactory
        from stock.models import MovementType

        yard = YardFactory(name="Main yard")
        store = StoreFactory(name="Store B")
        yard_node = node_for_location(yard)
        owner.full_name = "Olga Owner"
        owner.save()
        ctx = EventContext(actor=owner)
        jumper = ItemTypeFactory(name="Jumper", default_tracking_mode=TrackingMode.BULK)

        units = []
        for _ in range(3):
            unit = SerialUnitFactory(current_node=external_node(organization.pk))
            post(
                item_type=unit.item_type,
                quantity=Decimal("1"),
                from_node=external_node(organization.pk),
                to_node=yard_node,
                movement_type=MovementType.RECEIPT,
                tracking_mode=TrackingMode.SERIALIZED,
                serial_unit=unit,
                owner_type=unit.owner_type,
                owner_client=unit.owner_client,
            )
            unit.refresh_from_db()
            units.append(unit)
        post(
            item_type=jumper,
            quantity=Decimal("40"),
            from_node=external_node(organization.pk),
            to_node=yard_node,
            movement_type=MovementType.RECEIPT,
        )

        gate_in = GateIn.objects.create(
            source_type="PURCHASE",
            supplier_name="Acme",
            to_location=yard,
            received_at=timezone.now(),
            number="GRN-000001",
            status="POSTED",
        )
        pallet = create_box(code="PAL-1", node=yard_node, gate_in=gate_in, ctx=ctx)
        carton = create_box(code="CTN-1", node=yard_node, parent=pallet, ctx=ctx)
        put_units(carton, units[:2], ctx=ctx)
        put_units(pallet, units[2:], ctx=ctx)
        put_bulk(
            carton,
            item_type=jumper,
            owner_client=None,
            condition="NEW",
            quantity=Decimal("10"),
            ctx=ctx,
        )
        lone = create_box(code="LONE-1", node=yard_node, ctx=ctx)
    return {
        "yard": yard,
        "store": store,
        "units": units,
        "jumper": jumper,
        "gate_in": gate_in,
        "pallet": pallet,
        "carton": carton,
        "lone": lone,
    }


@pytest.fixture
def storekeeper_less(signed_in):
    """A signed-in user whose only right is to raise gate-outs."""
    http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
        from accounts.permissions_registry import PERM

        user = UserFactory(organization=organization, email="clerk@silvertech.co.ke")
        user.set_password("a good long password")
        user.save()
        UserRoleFactory(
            user=user, role=RoleFactory(name="Requester", codenames=[PERM.GATE_OUT_REQUEST])
        )
    return login_as(http, "clerk@silvertech.co.ke")


class TestList:
    def test_rows_carry_counts_and_nothing_more_per_row(
        self, signed_in, world, django_assert_max_num_queries
    ):
        http, token, _org, _owner = signed_in

        with django_assert_max_num_queries(12):
            rows = http.get(reverse("v1:box-list"), **auth(token)).json()["results"]

        by_code = {row["code"]: row for row in rows}
        assert set(by_code) == {"PAL-1", "CTN-1", "LONE-1"}
        assert set(by_code["PAL-1"]) == {
            "id",
            "code",
            "status",
            "source",
            "depth",
            "parent_code",
            "node",
            "node_label",
            "units_now",
            "bulk_lines_now",
            "created_at",
            "closed_at",
        }
        # The pallet counts what is in its carton too.
        assert (by_code["PAL-1"]["units_now"], by_code["PAL-1"]["bulk_lines_now"]) == (3, 1)
        assert (by_code["CTN-1"]["units_now"], by_code["CTN-1"]["bulk_lines_now"]) == (2, 1)
        assert by_code["CTN-1"]["parent_code"] == "PAL-1"
        assert by_code["LONE-1"]["units_now"] == 0
        assert by_code["LONE-1"]["parent_code"] is None

    def test_the_query_count_does_not_grow_with_the_boxes(self, signed_in, world):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        http, token, organization, owner = signed_in
        url = reverse("v1:box-list")

        def queries():
            with CaptureQueriesContext(connection) as captured:
                assert http.get(url, **auth(token)).status_code == 200
            return len(captured)

        queries()  # warm per-process caches
        before = queries()
        with tenant_context(organization):
            from stock.box_hooks import EventContext
            from stock.boxes import create_box

            for index in range(5):
                create_box(
                    code=f"MORE-{index}",
                    node=world["yard"].node,
                    ctx=EventContext(actor=owner),
                )
        assert queries() == before

    def test_filters(self, signed_in, world):
        http, token, _org, _owner = signed_in
        url = reverse("v1:box-list")

        def codes(**params):
            rows = http.get(url, params, **auth(token)).json()["results"]
            return {row["code"] for row in rows}

        yard = world["yard"]
        assert codes(node=yard.node.pk) == {"PAL-1", "CTN-1", "LONE-1"}
        assert codes(location=yard.pk) == {"PAL-1", "CTN-1", "LONE-1"}
        assert codes(location=world["store"].pk) == set()
        assert codes(gate_in=world["gate_in"].pk) == {"PAL-1"}
        assert codes(status="OPEN") == {"PAL-1", "CTN-1", "LONE-1"}
        assert codes(status="CLOSED") == set()
        assert codes(search="ctn") == {"CTN-1"}


class TestDetail:
    def test_found_by_code_in_any_case_with_tree_and_path(self, signed_in, world):
        http, token, _org, _owner = signed_in

        response = http.get(reverse("v1:box-detail", args=["ctn-1"]), **auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["code"] == "CTN-1"
        assert body["parent_code"] == "PAL-1"
        assert body["path"] == ["PAL-1", "CTN-1"]
        assert body["counts"]["now"]["units"] == 2
        assert len(body["units"]) == 2
        assert body["bulk"][0]["item_name"] == "Jumper"
        assert body["gate_in"] is None

        pallet = http.get(reverse("v1:box-detail", args=["PAL-1"]), **auth(token)).json()
        assert pallet["gate_in"] == world["gate_in"].pk
        assert pallet["gate_in_number"] == "GRN-000001"
        assert pallet["path"] == ["PAL-1"]
        assert pallet["children"][0]["code"] == "CTN-1"
        assert pallet["counts"]["now"]["units"] == 3

    def test_an_unknown_code_is_a_404(self, signed_in, world):
        http, token, _org, _owner = signed_in
        response = http.get(reverse("v1:box-detail", args=["NOPE"]), **auth(token))
        assert response.status_code == 404

    def test_a_dotted_code_is_addressable(self, signed_in, world):
        http, token, organization, owner = signed_in
        with tenant_context(organization):
            from stock.box_hooks import EventContext
            from stock.boxes import create_box

            create_box(
                code="SUP.77-A_1",
                node=world["yard"].node,
                ctx=EventContext(actor=owner),
            )
        response = http.get(reverse("v1:box-detail", args=["SUP.77-A_1"]), **auth(token))
        assert response.status_code == 200

    def test_another_tenants_box_is_a_404(self, signed_in, world, client):
        _http, _token, _org, _owner = signed_in
        rival = provision_tenant(name="Rival", slug="rival", owner_email="owner@rival.co.ke")
        rival["owner"].set_password("a good long password")
        rival["owner"].save()
        client.defaults["HTTP_HOST"] = "rival.localhost"
        token = login_as(client, "owner@rival.co.ke")

        detail = client.get(reverse("v1:box-detail", args=["PAL-1"]), **auth(token))
        assert detail.status_code == 404
        assert client.get(reverse("v1:box-list"), **auth(token)).json()["results"] == []
        found = client.get(reverse("v1:stock-lookup"), {"q": "PAL-1"}, **auth(token))
        assert found.status_code == 404

    def test_post_to_the_collection_is_refused(self, signed_in, world):
        http, token, _org, _owner = signed_in
        response = http.post(
            reverse("v1:box-list"), {"code": "X"}, content_type="application/json", **auth(token)
        )
        assert response.status_code == 405


class TestHistory:
    def test_newest_first_with_the_pallets_cartons_included(self, signed_in, world):
        http, token, _org, _owner = signed_in

        body = http.get(reverse("v1:box-history", args=["pal-1"]), **auth(token)).json()

        rows = body["results"]
        stamps = [row["occurred_at"] for row in rows]
        assert stamps == sorted(stamps, reverse=True)
        actions = {row["action"] for row in rows}
        assert {"CREATED", "UNIT_IN", "BULK_IN"} <= actions
        assert {row["box_code"] for row in rows} == {"PAL-1", "CTN-1"}
        unit_in = next(r for r in rows if r["action"] == "UNIT_IN")
        assert set(unit_in) == {
            "id",
            "occurred_at",
            "action",
            "action_label",
            "box_code",
            "actor",
            "serial_number",
            "child_box_code",
            "item_name",
            "owner_client",
            "condition",
            "quantity",
            "document_type",
            "document_id",
            "document_number",
            "note",
        }
        assert unit_in["action_label"] == "Unit put in"
        assert unit_in["serial_number"]
        assert unit_in["actor"] == "Olga Owner"
        bulk_in = next(r for r in rows if r["action"] == "BULK_IN")
        assert bulk_in["item_name"] == "Jumper"
        assert Decimal(bulk_in["quantity"]) == Decimal("10")

    def test_a_cartons_history_is_its_own(self, signed_in, world):
        http, token, _org, _owner = signed_in
        rows = http.get(reverse("v1:box-history", args=["CTN-1"]), **auth(token)).json()["results"]
        assert {row["box_code"] for row in rows} == {"CTN-1"}

    def test_it_pages(self, signed_in, world):
        http, token, _org, _owner = signed_in
        body = http.get(
            reverse("v1:box-history", args=["PAL-1"]), {"page_size": 2}, **auth(token)
        ).json()
        assert len(body["results"]) == 2
        assert body["next"]


class TestChangingABox:
    def test_take_out_a_unit_and_some_bulk(self, signed_in, world):
        http, token, _org, _owner = signed_in
        carton_unit = world["units"][0]

        response = http.post(
            reverse("v1:box-take-out", args=["ctn-1"]),
            {
                "units": [carton_unit.pk],
                "bulk": [
                    {
                        "item_type": world["jumper"].pk,
                        "owner_client": None,
                        "condition": "NEW",
                        "quantity": "4",
                    }
                ],
            },
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["counts"]["now"]["units"] == 1
        assert Decimal(body["bulk"][0]["quantity"]) == Decimal("6")
        history = http.get(reverse("v1:box-history", args=["CTN-1"]), **auth(token)).json()
        assert {"UNIT_OUT", "BULK_OUT"} <= {r["action"] for r in history["results"]}

    def test_take_out_a_child_box(self, signed_in, world):
        http, token, _org, _owner = signed_in

        response = http.post(
            reverse("v1:box-take-out", args=["PAL-1"]),
            {"boxes": ["ctn-1"]},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
        assert response.json()["children"] == []
        carton = http.get(reverse("v1:box-detail", args=["CTN-1"]), **auth(token)).json()
        assert carton["parent_code"] is None
        assert carton["path"] == ["CTN-1"]

    def test_a_unit_that_is_not_in_the_box_is_refused_in_the_envelope(self, signed_in, world):
        http, token, _org, _owner = signed_in

        response = http.post(
            reverse("v1:box-take-out", args=["CTN-1"]),
            {"units": [world["units"][2].pk]},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 409 or response.status_code == 400
        assert response.json()["error"]["code"] == "NOT_IN_THIS_BOX"

    def test_naming_nothing_is_refused(self, signed_in, world):
        http, token, _org, _owner = signed_in
        response = http.post(
            reverse("v1:box-take-out", args=["CTN-1"]),
            {},
            content_type="application/json",
            **auth(token),
        )
        assert response.status_code >= 400
        assert "error" in response.json()

    def test_an_unknown_unit_id_is_a_400(self, signed_in, world):
        http, token, _org, _owner = signed_in
        response = http.post(
            reverse("v1:box-take-out", args=["CTN-1"]),
            {"units": [999999]},
            content_type="application/json",
            **auth(token),
        )
        assert response.status_code == 400
        assert "units" in response.json()["error"]["field_errors"]

    def test_empty_closes_the_box(self, signed_in, world):
        http, token, _org, _owner = signed_in

        response = http.post(reverse("v1:box-empty", args=["CTN-1"]), **auth(token))

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["status"] == "CLOSED"
        assert body["counts"]["now"] == {"units": 0, "bulk": 0}
        assert body["closed_at"]

    def test_move_carries_the_box_and_its_contents(self, signed_in, world):
        http, token, _org, _owner = signed_in

        response = http.post(
            reverse("v1:box-move", args=["pal-1"]),
            {"to_location": world["store"].pk},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["node_label"] == "Store B"
        assert body["counts"]["now"]["units"] == 3
        carton = http.get(reverse("v1:box-detail", args=["CTN-1"]), **auth(token)).json()
        assert carton["node_label"] == "Store B"

    def test_move_to_a_location_that_does_not_exist_is_a_400(self, signed_in, world):
        http, token, _org, _owner = signed_in
        response = http.post(
            reverse("v1:box-move", args=["PAL-1"]),
            {"to_location": 999999},
            content_type="application/json",
            **auth(token),
        )
        assert response.status_code == 400

    def test_a_user_without_the_right_is_refused(self, signed_in, world, storekeeper_less):
        http, _token, _org, _owner = signed_in
        token = storekeeper_less

        for name, body in (
            ("v1:box-take-out", {"units": [world["units"][0].pk]}),
            ("v1:box-empty", {}),
            ("v1:box-move", {"to_location": world["store"].pk}),
        ):
            response = http.post(
                reverse(name, args=["CTN-1"]),
                body,
                content_type="application/json",
                **auth(token),
            )
            assert response.status_code == 403, name

        # Reading is open to any signed-in user, as /stock is.
        assert http.get(reverse("v1:box-detail", args=["CTN-1"]), **auth(token)).status_code == 200

    def test_gate_in_post_alone_is_enough(self, signed_in, world):
        http, _token, organization, _owner = signed_in
        with tenant_context(organization):
            from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
            from accounts.permissions_registry import PERM

            user = UserFactory(organization=organization, email="receiver@silvertech.co.ke")
            user.set_password("a good long password")
            user.save()
            UserRoleFactory(
                user=user, role=RoleFactory(name="Receiver", codenames=[PERM.GATE_IN_POST])
            )
        token = login_as(http, "receiver@silvertech.co.ke")

        response = http.post(reverse("v1:box-empty", args=["LONE-1"]), **auth(token))

        assert response.status_code == 200, response.content


class TestIssuable:
    def test_proposes_lines_and_says_what_cannot_go(self, signed_in, world):
        http, token, _org, _owner = signed_in

        response = http.get(
            reverse("v1:box-issuable", args=["pal-1"]),
            {"from_location": world["yard"].pk},
            **auth(token),
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert set(body) == {"lines", "excluded"}
        assert sum(len(line["units"]) for line in body["lines"]) == 3
        assert any(line["box_code"] == "CTN-1" and line["units"] for line in body["lines"])
        assert body["excluded"] == []

    def test_from_another_place_everything_is_excluded_as_not_here(self, signed_in, world):
        http, token, _org, _owner = signed_in

        body = http.get(
            reverse("v1:box-issuable", args=["PAL-1"]),
            {"from_location": world["store"].pk},
            **auth(token),
        ).json()

        assert body["lines"] == []
        assert {row["reason"] for row in body["excluded"]} == {"NOT_HERE"}

    @pytest.mark.parametrize("params", [{}, {"from_location": "abc"}, {"from_location": 999999}])
    def test_from_location_is_required_and_must_be_yours(self, signed_in, world, params):
        http, token, _org, _owner = signed_in
        response = http.get(reverse("v1:box-issuable", args=["PAL-1"]), params, **auth(token))
        assert response.status_code == 400
        assert "from_location" in response.json()["error"]["field_errors"]

    def test_it_needs_the_right_to_request_a_gate_out(self, signed_in, world):
        http, _token, organization, _owner = signed_in
        with tenant_context(organization):
            from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
            from accounts.permissions_registry import PERM

            user = UserFactory(organization=organization, email="viewer@silvertech.co.ke")
            user.set_password("a good long password")
            user.save()
            UserRoleFactory(
                user=user, role=RoleFactory(name="Adjuster", codenames=[PERM.STOCK_ADJUST])
            )
        token = login_as(http, "viewer@silvertech.co.ke")

        response = http.get(
            reverse("v1:box-issuable", args=["PAL-1"]),
            {"from_location": world["yard"].pk},
            **auth(token),
        )

        assert response.status_code == 403

    def test_another_tenants_box_is_a_404(self, signed_in, world, client):
        rival = provision_tenant(name="Rival", slug="rival", owner_email="owner@rival.co.ke")
        rival["owner"].set_password("a good long password")
        rival["owner"].save()
        client.defaults["HTTP_HOST"] = "rival.localhost"
        token = login_as(client, "owner@rival.co.ke")

        response = client.get(
            reverse("v1:box-issuable", args=["PAL-1"]), {"from_location": 1}, **auth(token)
        )

        assert response.status_code == 404


class TestLookup:
    def lookup(self, http, token, q):
        return http.get(reverse("v1:stock-lookup"), {"q": q}, **auth(token))

    def test_a_raw_code_in_any_case(self, signed_in, world):
        http, token, _org, _owner = signed_in

        body = self.lookup(http, token, "ctn-1").json()

        assert body["kind"] == "box"
        assert body["resource"] == "/stock/boxes/CTN-1"
        assert body["object"]["code"] == "CTN-1"
        assert body["object"]["units_now"] == 2
        assert body["object"]["parent_code"] == "PAL-1"

    def test_a_json_label(self, signed_in, world):
        http, token, _org, _owner = signed_in
        body = self.lookup(http, token, '{"carton": "PAL-1"}').json()
        assert (body["kind"], body["object"]["code"]) == ("box", "PAL-1")

    def test_a_gs1_sscc(self, signed_in, world):
        http, token, organization, owner = signed_in
        with tenant_context(organization):
            from stock.box_hooks import EventContext
            from stock.boxes import create_box

            create_box(
                code="003456780000000018",
                node=world["yard"].node,
                ctx=EventContext(actor=owner),
            )

        body = self.lookup(http, token, "(00)003456780000000018").json()

        assert (body["kind"], body["object"]["code"]) == ("box", "003456780000000018")

    def test_a_serial_still_wins_over_a_box_of_the_same_code(self, signed_in, world):
        http, token, organization, owner = signed_in
        serial = world["units"][0].serial_number
        with tenant_context(organization):
            from stock.box_hooks import EventContext
            from stock.boxes import create_box

            create_box(code=serial, node=world["yard"].node, ctx=EventContext(actor=owner))

        assert self.lookup(http, token, serial).json()["kind"] == "serial_unit"

    def test_unknown_is_a_404(self, signed_in, world):
        http, token, _org, _owner = signed_in
        assert self.lookup(http, token, "NO-SUCH").status_code == 404

