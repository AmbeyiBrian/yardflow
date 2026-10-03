"""T13.5, T13.5a — changing an earmark, earmarks on reads, what waits for a site
(§4.16.6, §4.16.7, §4.16.8a; Q2, Q4, Q6)."""

from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
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
    owner.full_name = "Olga Owner"
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


def post(**kwargs):
    from stock.services import MovementRequest, post_movement

    with transaction.atomic():
        return post_movement(MovementRequest(**kwargs))


def call(http, token, url, body):
    return http.post(url, body, content_type="application/json", **auth(token))


@pytest.fixture
def world(signed_in):
    """A yard and a store; two sites; three units and a drum earmarked for site A,
    40 ea of a bulk item (25 for A, 15 free) at the yard."""
    _http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        from catalogue.factories import ItemTypeFactory
        from catalogue.models import TrackingMode
        from locations.factories import StoreFactory, YardFactory
        from locations.nodes import external_node, node_for_location
        from network.factories import ClientFactory, SiteFactory
        from stock.factories import ReelFactory, SerialUnitFactory
        from stock.models import BulkEarmark, MovementType

        yard = YardFactory(name="Main yard")
        store = StoreFactory(name="Store B")
        yard_node = node_for_location(yard)
        ext = external_node(organization.pk)
        client_ = ClientFactory(name="Safaricom")
        site_a = SiteFactory(client=client_, name="Alpha site")
        site_b = SiteFactory(client=client_, name="Bravo site")

        units = []
        for _ in range(3):
            unit = SerialUnitFactory(current_node=ext)
            post(
                item_type=unit.item_type,
                quantity=Decimal("1"),
                from_node=ext,
                to_node=yard_node,
                movement_type=MovementType.RECEIPT,
                tracking_mode=TrackingMode.SERIALIZED,
                serial_unit=unit,
                owner_type=unit.owner_type,
                owner_client=unit.owner_client,
            )
            unit.refresh_from_db()
            unit.earmark_site = site_a
            unit.save()
            units.append(unit)

        drum = ReelFactory(current_node=ext)
        post(
            item_type=drum.item_type,
            quantity=drum.remaining_length,
            from_node=ext,
            to_node=yard_node,
            movement_type=MovementType.RECEIPT,
            tracking_mode=TrackingMode.REEL,
            reel=drum,
            owner_type=drum.owner_type,
        )
        drum.refresh_from_db()
        drum.earmark_site = site_a
        drum.save()

        jumper = ItemTypeFactory(name="Jumper", default_tracking_mode=TrackingMode.BULK)
        post(
            item_type=jumper,
            quantity=Decimal("40"),
            from_node=ext,
            to_node=yard_node,
            movement_type=MovementType.RECEIPT,
        )
        BulkEarmark.objects.create(
            organization=organization,
            site=site_a,
            node=yard_node,
            item_type=jumper,
            condition="NEW",
            quantity=Decimal("25"),
        )
    return {
        "yard": yard,
        "store": store,
        "yard_node": yard_node,
        "units": units,
        "drum": drum,
        "jumper": jumper,
        "site_a": site_a,
        "site_b": site_b,
        "client": client_,
    }


def change_url():
    return reverse("v1:earmark-change")


class TestChangeUnitAndDrum:
    def test_a_unit_is_moved_to_another_site(self, signed_in, world):
        http, token, organization, _owner = signed_in
        unit = world["units"][0]
        r = call(
            http,
            token,
            change_url(),
            {"serial_unit": unit.pk, "to_site": world["site_b"].pk, "reason": "Plan changed"},
        )
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["kind"] == "unit"
        assert body["unit"]["earmark_site"] == world["site_b"].pk
        assert body["unit"]["earmark_site_name"] == "Bravo site"
        with tenant_context(organization):
            from core.models import AuditLog
            from stock.models import EarmarkAction, EarmarkEvent

            event = EarmarkEvent.objects.get(serial_unit=unit)
            assert event.action == EarmarkAction.CHANGED
            assert (event.site_id, event.to_site_id) == (world["site_a"].pk, world["site_b"].pk)
            assert event.reason == "Plan changed"
            assert event.actor is not None
            assert AuditLog.objects.filter(note__contains="Plan changed").count() == 1

    def test_a_unit_is_cleared(self, signed_in, world):
        http, token, organization, _owner = signed_in
        unit = world["units"][0]
        r = call(
            http,
            token,
            change_url(),
            {"serial_unit": unit.pk, "to_site": None, "reason": "Not needed"},
        )
        assert r.status_code == 200
        assert r.json()["unit"]["earmark_site"] is None
        with tenant_context(organization):
            from stock.models import EarmarkEvent

            assert EarmarkEvent.objects.get(serial_unit=unit).action == "CLEARED"

    def test_a_drum_is_moved_and_cleared(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        drum = world["drum"]
        r = call(
            http,
            token,
            change_url(),
            {"reel": drum.pk, "to_site": world["site_b"].pk, "reason": "Re-plan"},
        )
        assert r.status_code == 200
        assert r.json()["drum"]["earmark_site_name"] == "Bravo site"
        r = call(http, token, change_url(), {"reel": drum.pk, "to_site": None, "reason": "Free"})
        assert r.json()["drum"]["earmark_site"] is None

    def test_a_reason_is_required(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        r = call(
            http,
            token,
            change_url(),
            {"serial_unit": world["units"][0].pk, "to_site": None, "reason": "  "},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EARMARK_CHANGE_INVALID"

    def test_clearing_an_unearmarked_unit_is_refused(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        unit = world["units"][0]
        unit.earmark_site = None
        unit.save()
        r = call(
            http, token, change_url(), {"serial_unit": unit.pk, "to_site": None, "reason": "x"}
        )
        assert r.status_code == 400
        assert unit.serial_number in r.json()["error"]["message"]

    def test_the_same_site_is_refused(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        r = call(
            http,
            token,
            change_url(),
            {"serial_unit": world["units"][0].pk, "to_site": world["site_a"].pk, "reason": "x"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EARMARK_CHANGE_INVALID"

    def test_a_unit_outside_the_yard_is_refused(self, signed_in, world):
        http, token, organization, _owner = signed_in
        with tenant_context(organization):
            from locations.nodes import external_node

            unit = world["units"][0]
            unit.current_node = external_node(organization.pk)
            unit.save()
        r = call(
            http,
            token,
            change_url(),
            {"serial_unit": unit.pk, "to_site": world["site_b"].pk, "reason": "x"},
        )
        assert r.status_code == 400
        assert "not inside the yard" in r.json()["error"]["message"]

    def test_two_subjects_or_none_are_refused(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        both = {
            "serial_unit": world["units"][0].pk,
            "reel": world["drum"].pk,
            "to_site": None,
            "reason": "x",
        }
        assert call(http, token, change_url(), both).status_code == 400
        assert call(http, token, change_url(), {"to_site": None, "reason": "x"}).status_code == 400


def lot(world, **extra):
    return {
        "node": world["yard_node"].pk,
        "item_type": world["jumper"].pk,
        "condition": "NEW",
        **extra,
    }


class TestChangeBulk:
    def test_part_of_a_claim_moves_to_another_site(self, signed_in, world):
        http, token, organization, _owner = signed_in
        body = lot(
            world,
            from_site=world["site_a"].pk,
            quantity="10",
            to_site=world["site_b"].pk,
            reason="Split",
        )
        r = call(http, token, change_url(), body)
        assert r.status_code == 200, r.content
        state = r.json()["bulk"]
        assert {e["name"]: e["quantity"] for e in state["earmarked"]} == {
            "Alpha site": "15.000",
            "Bravo site": "10.000",
        }
        assert state["free"] == "15.000"
        with tenant_context(organization):
            from stock.models import EarmarkEvent

            event = EarmarkEvent.objects.get(item_type=world["jumper"])
            assert (event.action, event.quantity, event.reason) == ("CHANGED", 10, "Split")

    def test_a_whole_claim_cleared_is_deleted(self, signed_in, world):
        http, token, organization, _owner = signed_in
        r = call(
            http,
            token,
            change_url(),
            lot(world, from_site=world["site_a"].pk, quantity="25", to_site=None, reason="Free"),
        )
        assert r.status_code == 200
        assert r.json()["bulk"]["earmarked"] == []
        assert r.json()["bulk"]["free"] == "40.000"
        with tenant_context(organization):
            from stock.models import BulkEarmark, EarmarkEvent

            assert not BulkEarmark.objects.exists()
            assert EarmarkEvent.objects.get().action == "CLEARED"

    def test_more_than_the_claim_is_refused(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        r = call(
            http,
            token,
            change_url(),
            lot(
                world,
                from_site=world["site_a"].pk,
                quantity="26",
                to_site=world["site_b"].pk,
                reason="x",
            ),
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "EARMARK_CHANGE_INVALID"
        assert "25" in r.json()["error"]["message"]

    def test_free_stock_is_earmarked_up_to_the_free_quantity(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        ok = call(
            http,
            token,
            change_url(),
            lot(world, from_site=None, quantity="15", to_site=world["site_b"].pk, reason="Plan"),
        )
        assert ok.status_code == 200, ok.content
        assert ok.json()["bulk"]["free"] == "0.000"
        over = call(
            http,
            token,
            change_url(),
            lot(world, from_site=None, quantity="1", to_site=world["site_b"].pk, reason="Plan"),
        )
        assert over.status_code == 400
        assert "free" in over.json()["error"]["message"]

    def test_free_to_a_site_with_an_existing_claim_adds_to_it(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        r = call(
            http,
            token,
            change_url(),
            lot(world, from_site=None, quantity="5", to_site=world["site_a"].pk, reason="More"),
        )
        assert r.status_code == 200
        assert r.json()["bulk"]["earmarked"][0]["quantity"] == "30.000"

    def test_free_to_free_is_refused(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        r = call(
            http,
            token,
            change_url(),
            lot(world, from_site=None, quantity="5", to_site=None, reason="x"),
        )
        assert r.status_code == 400

    def test_a_lot_outside_the_yard_is_refused(self, signed_in, world):
        http, token, organization, _owner = signed_in
        with tenant_context(organization):
            from locations.nodes import external_node

            ext = external_node(organization.pk)
        body = lot(world, from_site=None, quantity="1", to_site=world["site_b"].pk, reason="x")
        body["node"] = ext.pk
        assert call(http, token, change_url(), body).status_code == 400


class TestPermission:
    def _user(self, organization, codenames, email):
        with tenant_context(organization):
            from accounts.factories import RoleFactory, UserFactory, UserRoleFactory

            user = UserFactory(organization=organization, email=email)
            user.set_password("a good long password")
            user.save()
            UserRoleFactory(user=user, role=RoleFactory(name=email, codenames=codenames))

    def _login(self, http, email):
        return http.post(
            reverse("v1:auth:login"),
            {"identifier": email, "password": "a good long password"},
            content_type="application/json",
        ).json()["access"]

    def test_change_needs_stock_adjust_or_gate_in_post(self, signed_in, world):
        from accounts.permissions_registry import PERM

        http, _token, organization, _owner = signed_in
        self._user(organization, [PERM.GATE_OUT_REQUEST], "clerk@silvertech.co.ke")
        self._user(organization, [PERM.GATE_IN_POST], "receiver@silvertech.co.ke")
        body = {"serial_unit": world["units"][0].pk, "to_site": None, "reason": "x"}

        clerk = self._login(http, "clerk@silvertech.co.ke")
        assert call(http, clerk, change_url(), body).status_code == 403
        receiver = self._login(http, "receiver@silvertech.co.ke")
        assert call(http, receiver, change_url(), body).status_code == 200

    def test_the_site_endpoint_needs_gate_out_request(self, signed_in, world):
        from accounts.permissions_registry import PERM

        http, _token, organization, _owner = signed_in
        self._user(organization, [PERM.GATE_IN_POST], "receiver@silvertech.co.ke")
        self._user(organization, [PERM.GATE_OUT_REQUEST], "clerk@silvertech.co.ke")
        url = reverse("v1:earmarked-for-site")
        query = {"site": world["site_a"].pk, "from_location": world["yard"].pk}
        receiver = self._login(http, "receiver@silvertech.co.ke")
        assert http.get(url, query, **auth(receiver)).status_code == 403
        clerk = self._login(http, "clerk@silvertech.co.ke")
        assert http.get(url, query, **auth(clerk)).status_code == 200


class TestReads:
    def test_units_and_drums_carry_their_site(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        units = http.get(reverse("v1:serial-list"), **auth(token)).json()["results"]
        assert {u["earmark_site_name"] for u in units} == {"Alpha site"}
        drums = http.get(reverse("v1:drum-list"), **auth(token)).json()["results"]
        assert drums[0]["earmark_site"] == world["site_a"].pk
        assert drums[0]["earmark_site_name"] == "Alpha site"

    def test_stock_rows_carry_the_split(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        rows = http.get(reverse("v1:stock-on-hand"), **auth(token)).json()["results"]
        by_item = {r["item_name"]: r for r in rows}
        jumper = by_item["Jumper"]
        assert jumper["earmarked"] == [
            {"site": world["site_a"].pk, "name": "Alpha site", "quantity": "25.000"}
        ]
        assert jumper["free"] == "15.000"
        unclaimed = next(r for r in rows if r["item_name"] != "Jumper")
        assert unclaimed["earmarked"] == []
        assert unclaimed["free"] == unclaimed["quantity"]

    def test_the_query_count_does_not_grow_with_the_rows(self, signed_in, world):
        http, token, organization, _owner = signed_in
        url = reverse("v1:stock-on-hand")

        def queries():
            with CaptureQueriesContext(connection) as captured:
                assert http.get(url, **auth(token)).status_code == 200
            return len(captured)

        queries()
        before = queries()
        with tenant_context(organization):
            from catalogue.factories import ItemTypeFactory
            from catalogue.models import TrackingMode
            from locations.nodes import external_node
            from stock.models import BulkEarmark, MovementType

            for n in range(5):
                item = ItemTypeFactory(name=f"Extra {n}", default_tracking_mode=TrackingMode.BULK)
                post(
                    item_type=item,
                    quantity=Decimal("10"),
                    from_node=external_node(organization.pk),
                    to_node=world["yard_node"],
                    movement_type=MovementType.RECEIPT,
                )
                BulkEarmark.objects.create(
                    organization=organization,
                    site=world["site_b"],
                    node=world["yard_node"],
                    item_type=item,
                    condition="NEW",
                    quantity=Decimal("4"),
                )
        assert queries() == before


class TestHistory:
    def _change(self, http, token, world):
        call(
            http,
            token,
            change_url(),
            {
                "serial_unit": world["units"][0].pk,
                "to_site": world["site_b"].pk,
                "reason": "First",
            },
        )
        call(
            http,
            token,
            change_url(),
            {"serial_unit": world["units"][0].pk, "to_site": None, "reason": "Second"},
        )

    def test_a_units_history_is_newest_first_with_names(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        self._change(http, token, world)
        r = http.get(
            reverse("v1:earmark-history"), {"serial_unit": world["units"][0].pk}, **auth(token)
        )
        assert r.status_code == 200
        rows = r.json()["results"]
        assert [x["reason"] for x in rows] == ["Second", "First"]
        assert rows[1]["site_name"] == "Alpha site"
        assert rows[1]["to_site_name"] == "Bravo site"
        assert rows[0]["action"] == "CLEARED"
        assert rows[0]["actor_name"] == "Olga Owner"
        assert rows[0]["serial_number"] == world["units"][0].serial_number

    def test_a_sites_history_matches_either_side(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        self._change(http, token, world)
        url = reverse("v1:earmark-history")
        for site in (world["site_a"], world["site_b"]):
            rows = http.get(url, {"site": site.pk}, **auth(token)).json()["results"]
            assert len(rows) >= 1

    def test_a_drum_history(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        call(
            http,
            token,
            change_url(),
            {"reel": world["drum"].pk, "to_site": None, "reason": "Free"},
        )
        rows = http.get(
            reverse("v1:earmark-history"), {"reel": world["drum"].pk}, **auth(token)
        ).json()["results"]
        assert rows[0]["drum_number"] == world["drum"].drum_number

    def test_a_subject_is_required(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        assert http.get(reverse("v1:earmark-history"), **auth(token)).status_code == 400


@pytest.fixture
def jobs(signed_in, world):
    _http, _token, organization, owner = signed_in
    with tenant_context(organization):
        from jobs.models import Job, JobStatus
        from network.factories import ProjectFactory

        p1 = ProjectFactory(client=world["client"], title="Rollout 2026")
        p2 = ProjectFactory(client=world["client"], title="Swap-out")
        made = []
        for ref, project, status in (
            ("J-1", p1, JobStatus.OPEN),
            ("J-2", p2, JobStatus.IN_PROGRESS),
            ("J-3", p2, JobStatus.CANCELLED),
        ):
            made.append(
                Job.objects.create(
                    organization=organization,
                    reference=ref,
                    client=world["client"],
                    site=world["site_a"],
                    project=project,
                    assignee=owner,
                    status=status,
                )
            )
    return made


class TestWaitingForASite:
    def get(self, http, token, site, location):
        return http.get(
            reverse("v1:earmarked-for-site"),
            {"site": site.pk, "from_location": location.pk},
            **auth(token),
        )

    def test_units_a_drum_and_bulk_come_as_proposal_lines(self, signed_in, world, jobs):
        http, token, _organization, _owner = signed_in
        r = self.get(http, token, world["site_a"], world["yard"])
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["excluded"] == []
        by_mode = {}
        for line in body["lines"]:
            by_mode.setdefault(line["tracking_mode"], []).append(line)
        assert set(by_mode) == {"SERIALIZED", "REEL", "BULK"}

        serialized = by_mode["SERIALIZED"]
        assert sum(len(line["units"]) for line in serialized) == 3
        assert sum(Decimal(str(line["requested_qty"])) for line in serialized) == 3
        assert set(serialized[0]["units"][0]) == {"serial_unit", "serial_number"}

        (reel_line,) = by_mode["REEL"]
        assert len(reel_line["reels"]) == 1
        drum = reel_line["reels"][0]
        assert drum["reel"] == world["drum"].pk
        assert drum["drum_number"] == world["drum"].drum_number
        assert Decimal(str(drum["length"])) == Decimal("500")
        assert Decimal(str(reel_line["requested_qty"])) == Decimal("500")
        assert reel_line["uom"] == "m"

        (bulk,) = by_mode["BULK"]
        assert Decimal(str(bulk["requested_qty"])) == Decimal("25")
        assert bulk["item_name"] == "Jumper"
        assert bulk["owner_type"] == "OWN" and bulk["owner_client"] is None
        assert bulk["condition"] == "NEW"
        for line in body["lines"]:
            assert {
                "item_type",
                "item_name",
                "tracking_mode",
                "uom",
                "owner_type",
                "owner_client",
                "condition",
                "requested_qty",
                "units",
            } <= set(line)

    def test_open_jobs_come_with_their_projects(self, signed_in, world, jobs):
        http, token, _organization, _owner = signed_in
        body = self.get(http, token, world["site_a"], world["yard"]).json()
        assert [(j["reference"], j["project_name"]) for j in body["jobs"]] == [
            ("J-1", "Rollout 2026"),
            ("J-2", "Swap-out"),
        ]
        assert {j["project"] for j in body["jobs"]} == {jobs[0].project_id, jobs[1].project_id}

    def test_a_unit_that_cannot_go_is_excluded_with_a_reason(self, signed_in, world, jobs):
        http, token, _organization, _owner = signed_in
        quarantined = world["units"][0]
        quarantined.status = "QUARANTINED"
        quarantined.save()
        body = self.get(http, token, world["site_a"], world["yard"]).json()
        assert [(e["reason"], e["serial_number"]) for e in body["excluded"]] == [
            ("QUARANTINED", quarantined.serial_number)
        ]
        listed = [u["serial_number"] for line in body["lines"] for u in line["units"]]
        assert quarantined.serial_number not in listed
        assert len(listed) == 2

    def test_stock_at_another_place_is_not_listed(self, signed_in, world, jobs):
        http, token, _organization, _owner = signed_in
        body = self.get(http, token, world["site_a"], world["store"]).json()
        assert body["lines"] == []
        assert body["excluded"] == []
        assert len(body["jobs"]) == 2

    def test_a_site_with_nothing_earmarked_is_an_empty_list(self, signed_in, world, jobs):
        http, token, _organization, _owner = signed_in
        body = self.get(http, token, world["site_b"], world["yard"]).json()
        assert body == {"lines": [], "excluded": [], "jobs": []}

    def test_a_missing_site_or_location_is_a_400(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        url = reverse("v1:earmarked-for-site")
        assert http.get(url, {"from_location": world["yard"].pk}, **auth(token)).status_code == 400
        assert http.get(url, {"site": world["site_a"].pk}, **auth(token)).status_code == 400
        r = http.get(url, {"site": 999999, "from_location": world["yard"].pk}, **auth(token))
        assert r.status_code == 400

    def test_another_tenants_site_or_location_is_refused(self, signed_in, world):
        http, token, _organization, _owner = signed_in
        other = provision_tenant(name="Rival", slug="rival", owner_email="owner@rival.co.ke")[
            "organization"
        ]
        with tenant_context(other):
            from locations.factories import YardFactory
            from network.factories import ClientFactory, SiteFactory

            rival_site = SiteFactory(client=ClientFactory(name="Rival op"), name="Rival site")
            rival_yard = YardFactory(name="Rival yard")
        url = reverse("v1:earmarked-for-site")
        r = http.get(
            url, {"site": rival_site.pk, "from_location": world["yard"].pk}, **auth(token)
        )
        assert r.status_code == 400
        r = http.get(
            url, {"site": world["site_a"].pk, "from_location": rival_yard.pk}, **auth(token)
        )
        assert r.status_code == 400
        # And a change cannot name another tenant's site.
        r = call(
            http,
            token,
            change_url(),
            {"serial_unit": world["units"][0].pk, "to_site": rival_site.pk, "reason": "x"},
        )
        assert r.status_code == 400
