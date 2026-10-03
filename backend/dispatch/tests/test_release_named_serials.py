"""T11.10 - release by named serials (P11, G1, design 4.15.8).

The gate ticks the units it actually loaded. Exactly those are issued, the rest
of the line is short and raises the usual variance, and an organization can make
naming them mandatory.
"""

from uuid import uuid4

import pytest
from django.urls import reverse

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from dispatch.models import GateOutLineSerial, ReleaseVariance
from dispatch.services import (
    ReleaseNotPermitted,
    ScanRequiredForRelease,
    release_gate_out,
    submit_gate_out,
)
from dispatch.tests.test_gate_out import add_line, make_gate_out, stock_in
from locations.factories import YardFactory
from stock.models import SerialUnit, SerialUnitStatus
from stock.services import MovementRequest, post_movement


def _units(tenant, yard, item, count, prefix="RRU"):
    """`count` serialized units received into the yard."""
    from django.db import transaction

    from locations.nodes import external_node
    from stock.models import MovementType

    units = []
    for n in range(count):
        unit = SerialUnit.objects.create(
            organization=tenant,
            item_type=item,
            serial_number=f"{prefix}-{n + 1}",
            # Starts outside and the receipt moves it in, as gate-in does
            # (T11.3: a movement starts where the unit is).
            current_node=external_node(tenant.pk),
        )
        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=1,
                    from_node=external_node(tenant.pk),
                    to_node=yard.node,
                    movement_type=MovementType.RECEIPT,
                    tracking_mode=TrackingMode.SERIALIZED,
                    serial_unit=unit,
                )
            )
        units.append(unit)
    return units


def _serialized_pass(tenant, yard, requester, holder, count=3):
    item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
    units = _units(tenant, yard, item, count)
    gate_out = make_gate_out(tenant, yard, requester, holder)
    line = add_line(gate_out, item, count, tracking_mode=TrackingMode.SERIALIZED)
    for unit in units:
        GateOutLineSerial.objects.create(organization=tenant, line=line, serial_unit=unit)
    submit_gate_out(gate_out, submitted_by=requester)
    gate_out.refresh_from_db()
    return gate_out, line, units


@pytest.fixture
def people(tenant):
    return UserFactory(organization=tenant), UserFactory(organization=tenant)


@pytest.fixture
def three(tenant, people):
    yard = YardFactory(name="Named yard")
    return _serialized_pass(tenant, yard, *people)


class TestNamedRelease:
    def test_exactly_the_named_units_are_issued(self, tenant, people, three):
        gate_out, line, units = three
        # Name the second and third, so "first N by id" would give the wrong answer.
        release_gate_out(
            gate_out,
            released_by=people[0],
            released_serials={line.pk: [units[2].pk, units[1].pk]},
            variance_reasons={line.pk: "One left on the dock"},
        )

        for unit in units:
            unit.refresh_from_db()
        assert units[0].current_node == gate_out.from_location.node
        assert units[0].status == SerialUnitStatus.IN_STOCK
        assert units[1].current_node != gate_out.from_location.node
        assert units[2].current_node != gate_out.from_location.node
        flags = {e.serial_unit_id: e.released for e in line.serials.all()}
        assert flags == {units[0].pk: False, units[1].pk: True, units[2].pk: True}
        line.refresh_from_db()
        assert line.released_qty == 2
        variance = ReleaseVariance.objects.get(gate_out_line=line)
        assert variance.released_qty == 2
        assert variance.reason == "One left on the dock"

    def test_string_keys_work_like_released_lines(self, tenant, people, three):
        gate_out, line, units = three
        release_gate_out(
            gate_out,
            released_by=people[0],
            released_serials={str(line.pk): [u.pk for u in units]},
        )
        line.refresh_from_db()
        assert line.released_qty == 3
        assert not ReleaseVariance.objects.exists()

    def test_an_empty_list_releases_nothing_and_is_short(self, tenant, people, three):
        gate_out, line, _units = three
        release_gate_out(gate_out, released_by=people[0], released_serials={line.pk: []})
        line.refresh_from_db()
        assert line.released_qty == 0
        assert ReleaseVariance.objects.filter(gate_out_line=line).count() == 1

    def test_a_unit_not_on_the_line_is_refused_by_name(self, tenant, people, three):
        gate_out, line, units = three
        other = YardFactory(name="Other")
        stranger = _units(tenant, other, line.item_type, 1, prefix="STRANGER")[0]
        with pytest.raises(ReleaseNotPermitted, match="STRANGER-1"):
            release_gate_out(
                gate_out,
                released_by=people[0],
                released_serials={line.pk: [units[0].pk, stranger.pk]},
            )
        units[0].refresh_from_db()
        assert units[0].current_node == gate_out.from_location.node

    def test_a_duplicate_is_refused(self, tenant, people, three):
        gate_out, line, units = three
        with pytest.raises(ReleaseNotPermitted, match="twice"):
            release_gate_out(
                gate_out,
                released_by=people[0],
                released_serials={line.pk: [units[0].pk, units[0].pk]},
            )

    def test_disagreeing_with_released_lines_is_refused(self, tenant, people, three):
        gate_out, line, units = three
        with pytest.raises(ReleaseNotPermitted, match="agree"):
            release_gate_out(
                gate_out,
                released_by=people[0],
                released_lines={line.pk: 3},
                released_serials={line.pk: [units[0].pk]},
            )

    def test_agreeing_quantity_is_accepted(self, tenant, people, three):
        gate_out, line, units = three
        release_gate_out(
            gate_out,
            released_by=people[0],
            released_lines={line.pk: 1},
            released_serials={line.pk: [units[0].pk]},
        )
        line.refresh_from_db()
        assert line.released_qty == 1

    def test_without_named_units_the_fallback_is_ordered_by_id(self, tenant, people, three):
        gate_out, line, units = three
        release_gate_out(gate_out, released_by=people[0], released_lines={line.pk: 1})
        released = line.serials.get(released=True)
        assert released.serial_unit_id == units[0].pk


class TestScanRequired:
    def _turn_on(self, tenant):
        tenant.settings.release_scan_required = True
        tenant.settings.save()

    def test_an_unscanned_serialized_release_is_refused(self, tenant, people, three):
        gate_out, _line, units = three
        self._turn_on(tenant)
        with pytest.raises(ScanRequiredForRelease) as raised:
            release_gate_out(gate_out, released_by=people[0])
        assert raised.value.code == "SCAN_REQUIRED_FOR_RELEASE"
        units[0].refresh_from_db()
        assert units[0].current_node == gate_out.from_location.node

    def test_naming_the_units_allows_it(self, tenant, people, three):
        gate_out, line, units = three
        self._turn_on(tenant)
        release_gate_out(
            gate_out,
            released_by=people[0],
            released_serials={line.pk: [u.pk for u in units]},
        )
        line.refresh_from_db()
        assert line.released_qty == 3

    def test_releasing_zero_of_a_serialized_line_needs_no_scan(self, tenant, people, three):
        gate_out, line, _units = three
        self._turn_on(tenant)
        release_gate_out(gate_out, released_by=people[0], released_lines={line.pk: 0})
        line.refresh_from_db()
        assert line.released_qty == 0

    def test_bulk_lines_are_not_affected(self, tenant, people):
        self._turn_on(tenant)
        yard = YardFactory(name="Bulk yard")
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, *people)
        add_line(gate_out, item, 4)
        submit_gate_out(gate_out, submitted_by=people[0])
        gate_out.refresh_from_db()
        release_gate_out(gate_out, released_by=people[0])
        assert gate_out.lines.get().released_qty == 4

    def test_setting_off_keeps_todays_behaviour(self, tenant, people, three):
        gate_out, line, _units = three
        release_gate_out(gate_out, released_by=people[0])
        line.refresh_from_db()
        assert line.released_qty == 3


# --------------------------------------------------------------------------
# API and offline replay
# --------------------------------------------------------------------------


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


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.fixture
def api_pass(signed_in):
    _http, _token, organization, owner = signed_in
    with tenant_context(organization):
        yard = YardFactory(name="Api yard")
        gate_out, line, units = _serialized_pass(organization, yard, owner, owner)
        ids = (gate_out.pk, line.pk, [u.pk for u in units])
    return organization, ids


class TestApiAndReplay:
    def test_the_api_releases_the_named_units(self, signed_in, api_pass):
        http, token, organization, _owner = signed_in
        _org, (gate_out_id, line_id, unit_ids) = api_pass

        response = http.post(
            reverse("v1:gate-out-release", args=[gate_out_id]),
            {
                "released_serials": {str(line_id): [unit_ids[1]]},
                "variance_reasons": {str(line_id): "Two stayed behind"},
            },
            content_type="application/json",
            **_auth(token),
        )

        assert response.status_code == 200, response.content
        with tenant_context(organization):
            released = list(
                GateOutLineSerial.objects.filter(line_id=line_id, released=True).values_list(
                    "serial_unit_id", flat=True
                )
            )
            assert released == [unit_ids[1]]

    def test_the_api_refuses_an_unscanned_release_when_required(self, signed_in, api_pass):
        http, token, organization, _owner = signed_in
        _org, (gate_out_id, _line_id, _unit_ids) = api_pass
        with tenant_context(organization):
            organization.settings.release_scan_required = True
            organization.settings.save()

        response = http.post(
            reverse("v1:gate-out-release", args=[gate_out_id]),
            {},
            content_type="application/json",
            **_auth(token),
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SCAN_REQUIRED_FOR_RELEASE"

    def test_a_queued_release_replays_with_its_named_units(self, signed_in, api_pass):
        http, token, organization, _owner = signed_in
        _org, (gate_out_id, line_id, unit_ids) = api_pass

        body = http.post(
            reverse("v1:sync-submissions"),
            {
                "submissions": [
                    {
                        "client_uuid": str(uuid4()),
                        "operation": "GATE_OUT_RELEASE",
                        "payload": {
                            "gate_out": gate_out_id,
                            "released_serials": {str(line_id): [unit_ids[2]]},
                            "variance_reasons": {str(line_id): "Short"},
                        },
                    }
                ]
            },
            content_type="application/json",
            **_auth(token),
        ).json()

        assert body["applied"] == 1, body
        with tenant_context(organization):
            released = list(
                GateOutLineSerial.objects.filter(line_id=line_id, released=True).values_list(
                    "serial_unit_id", flat=True
                )
            )
            assert released == [unit_ids[2]]
