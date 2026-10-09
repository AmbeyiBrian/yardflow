"""T17.10 — suppliers replayed from a phone, and the bundle's registers (§4.20.8).

The queue is a second door to ``add_supplier`` and to ``GateInSerializer``, so
what is pinned here is that it lands what the online form would, once, that a
refusal is a recorded exception carrying the domain code, that a gate-in can
name a supplier queued in the same batch, and that the bundle carries ids and
names only.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import UserFactory
from assets.models import Asset, AssetCloseReason, AssetStatus, AssetType
from core.tenancy import tenant_context
from locations.factories import YardFactory
from network.models import Supplier, SupplierStatus
from sync.models import ExceptionStatus, SubmissionStatus, SyncException, SyncSubmission
from sync.services import apply_submission, resolve_exception

pytestmark = pytest.mark.django_db


@pytest.fixture
def clerk(tenant):
    return UserFactory(organization=tenant, full_name="Gate Clerk")


def send(tenant, clerk, operation, payload, uuid=None, **kw):
    uuid = uuid or uuid4()
    return apply_submission(
        organization=tenant,
        client_uuid=uuid,
        operation=operation,
        payload={**payload, "client_uuid": str(uuid), **kw},
        submitted_by=clerk,
    )


def supplier_body(**extra):
    return {"name": "Kenya Cable Ltd", "phone": "0700111222", **extra}


def gate_in_body(yard, **extra):
    from catalogue.factories import ItemTypeFactory

    item = ItemTypeFactory(name="Sync clamp", uom="ea")
    return {
        "source_type": "PURCHASE",
        "to_location": yard.pk,
        "received_at": timezone.now().isoformat(),
        "lines": [
            {
                "item_type": item.pk,
                "tracking_mode": "BULK",
                "quantity": "10",
                "uom": "ea",
                "condition": "NEW",
            }
        ],
        **extra,
    }


class TestSupplierReplay:
    def test_it_lands_pending_as_the_online_form_would(self, tenant, clerk):
        body = supplier_body(kra_pin="P051234567A", bank_name="KCB", account_number="1")
        submission, replay = send(tenant, clerk, "SUPPLIER", body)

        assert not replay
        assert submission.status == SubmissionStatus.APPLIED
        supplier = Supplier.objects.get()
        assert submission.document_id == str(supplier.pk)
        assert submission.document_type == Supplier._meta.label
        assert supplier.status == SupplierStatus.PENDING
        assert supplier.registered_by == clerk
        assert supplier.client_uuid == submission.client_uuid
        assert supplier.kra_pin == "P051234567A"
        assert supplier.phone == "0700111222"

    def test_a_replay_returns_the_same_row(self, tenant, clerk):
        uuid = uuid4()
        first, _ = send(tenant, clerk, "SUPPLIER", supplier_body(), uuid)
        second, replay = send(tenant, clerk, "SUPPLIER", supplier_body(), uuid)

        assert replay
        assert second.pk == first.pk
        assert Supplier.objects.count() == 1

    def test_a_form_that_already_saved_online_is_not_doubled(self, tenant, clerk):
        from network.suppliers import add_supplier

        uuid = uuid4()
        made = add_supplier(actor=clerk, name="Kenya Cable Ltd", client_uuid=uuid)
        submission, _ = send(tenant, clerk, "SUPPLIER", supplier_body(), uuid)

        assert submission.status == SubmissionStatus.APPLIED
        assert submission.document_id == str(made.pk)
        assert Supplier.objects.count() == 1

    def test_unknown_keys_in_the_payload_are_ignored(self, tenant, clerk):
        submission, _ = send(
            tenant, clerk, "SUPPLIER", supplier_body(status="APPROVED", is_active=False)
        )

        assert submission.status == SubmissionStatus.APPLIED
        assert Supplier.objects.get().status == SupplierStatus.PENDING

    def test_a_duplicate_name_is_an_exception_with_its_code(self, tenant, clerk):
        send(tenant, clerk, "SUPPLIER", supplier_body())
        submission, _ = send(tenant, clerk, "SUPPLIER", supplier_body(name="kenya  cable ltd"))

        assert submission.status == SubmissionStatus.REJECTED
        exception = SyncException.objects.get()
        assert exception.code == "SUPPLIER_NAME_DUPLICATE"
        assert exception.details["existing"]["name"] == "Kenya Cable Ltd"
        assert Supplier.objects.count() == 1

    def test_a_duplicate_pin_is_an_exception_with_its_code(self, tenant, clerk):
        send(tenant, clerk, "SUPPLIER", supplier_body(kra_pin="P051234567A"))
        submission, _ = send(
            tenant, clerk, "SUPPLIER", supplier_body(name="Other Ltd", kra_pin="p051234567a")
        )

        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "SUPPLIER_PIN_DUPLICATE"

    def test_a_missing_name_is_refused(self, tenant, clerk):
        submission, _ = send(tenant, clerk, "SUPPLIER", {"phone": "0700"})

        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "SUPPLIER_NAME_REQUIRED"

    def test_fix_and_resend_closes_the_old_refusal(self, tenant, clerk):
        send(tenant, clerk, "SUPPLIER", supplier_body())
        bad = uuid4()
        send(tenant, clerk, "SUPPLIER", supplier_body(), bad)
        assert SyncException.objects.get().status == ExceptionStatus.OPEN

        send(
            tenant,
            clerk,
            "SUPPLIER",
            supplier_body(name="Kenya Cable Kenya"),
            supersedes_client_uuid=str(bad),
        )

        exception = SyncException.objects.get()
        assert exception.status == ExceptionStatus.RESOLVED
        assert exception.replacement_submission is not None
        assert Supplier.objects.count() == 2

    def test_a_refusal_can_be_resolved_like_any_other(self, tenant, clerk):
        send(tenant, clerk, "SUPPLIER", supplier_body())
        send(tenant, clerk, "SUPPLIER", supplier_body())
        exception = SyncException.objects.get()

        resolve_exception(exception, resolution="Used the existing one", discard=True)

        exception.refresh_from_db()
        assert exception.status == ExceptionStatus.DISCARDED


class TestGateInNamesASupplier:
    @pytest.fixture
    def yard(self, tenant):
        return YardFactory(name="Sync yard")

    def test_a_supplier_queued_in_the_same_batch(self, tenant, clerk, yard):
        from receiving.models import GateIn

        supplier_uuid = uuid4()
        send(tenant, clerk, "SUPPLIER", supplier_body(), supplier_uuid)
        submission, _ = send(
            tenant,
            clerk,
            "GATE_IN",
            gate_in_body(yard, supplier_client_uuid=str(supplier_uuid)),
        )

        assert submission.status == SubmissionStatus.APPLIED
        gate_in = GateIn.objects.get()
        supplier = Supplier.objects.get()
        assert gate_in.supplier == supplier
        assert gate_in.supplier_name == "Kenya Cable Ltd"

    def test_a_supplier_by_id(self, tenant, clerk, yard):
        from network.suppliers import add_supplier
        from receiving.models import GateIn

        supplier = add_supplier(actor=clerk, name="Kenya Cable Ltd")
        submission, _ = send(tenant, clerk, "GATE_IN", gate_in_body(yard, supplier=supplier.pk))

        assert submission.status == SubmissionStatus.APPLIED
        assert GateIn.objects.get().supplier == supplier

    def test_free_text_alone_still_works(self, tenant, clerk, yard):
        from receiving.models import GateIn

        submission, _ = send(
            tenant, clerk, "GATE_IN", gate_in_body(yard, supplier_name="Old queue draft")
        )

        assert submission.status == SubmissionStatus.APPLIED
        gate_in = GateIn.objects.get()
        assert gate_in.supplier is None
        assert gate_in.supplier_name == "Old queue draft"

    def test_when_the_supplier_was_refused_the_gate_in_waits_behind_it(
        self, tenant, clerk, yard
    ):
        from receiving.models import GateIn

        send(tenant, clerk, "SUPPLIER", supplier_body())
        dup = uuid4()
        send(tenant, clerk, "SUPPLIER", supplier_body(), dup)  # refused: duplicate name
        submission, _ = send(
            tenant, clerk, "GATE_IN", gate_in_body(yard, supplier_client_uuid=str(dup))
        )

        assert submission.status == SubmissionStatus.REJECTED
        codes = set(SyncException.objects.values_list("code", flat=True))
        assert codes == {"SUPPLIER_NAME_DUPLICATE", "SYNC_REFUSED"}
        assert not GateIn.objects.exists()

    def test_a_rejected_supplier_cannot_take_a_new_delivery(self, tenant, clerk, yard):
        from network.suppliers import add_supplier

        supplier = add_supplier(actor=clerk, name="Kenya Cable Ltd")
        Supplier.objects.filter(pk=supplier.pk).update(status=SupplierStatus.REJECTED)
        submission, _ = send(tenant, clerk, "GATE_IN", gate_in_body(yard, supplier=supplier.pk))

        assert submission.status == SubmissionStatus.REJECTED
        assert SyncException.objects.get().code == "SUPPLIER_NOT_USABLE_ON_GATE_IN"

    def test_a_pending_supplier_is_fine(self, tenant, clerk, yard):
        from network.suppliers import add_supplier

        supplier = add_supplier(actor=clerk, name="Kenya Cable Ltd")
        submission, _ = send(tenant, clerk, "GATE_IN", gate_in_body(yard, supplier=supplier.pk))

        assert submission.status == SubmissionStatus.APPLIED

    def test_another_tenants_supplier_is_not_found(
        self, tenant, other_organization, clerk, yard
    ):
        from network.suppliers import add_supplier

        with tenant_context(other_organization):
            theirs = add_supplier(
                actor=UserFactory(organization=other_organization), name="Theirs Ltd"
            )
        submission, _ = send(tenant, clerk, "GATE_IN", gate_in_body(yard, supplier=theirs.pk))

        assert submission.status == SubmissionStatus.REJECTED
        assert "not on the register" in SyncException.objects.get().reason


class TestBundle:
    @pytest.fixture
    def http(self, tenant, clerk, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        client = APIClient(HTTP_HOST="silvertech.localhost")
        client.force_authenticate(clerk)
        return client

    def test_suppliers_are_active_and_not_rejected_with_no_pin_or_payment(
        self, http, tenant, clerk
    ):
        from network.suppliers import add_supplier

        pending = add_supplier(
            actor=clerk,
            name="Pending Ltd",
            kra_pin="P051234567A",
            bank_name="KCB",
            account_number="777",
            mpesa_type="TILL",
            mpesa_number="123456",
        )
        approved = add_supplier(actor=clerk, name="Approved Ltd")
        rejected = add_supplier(actor=clerk, name="Rejected Ltd")
        inactive = add_supplier(actor=clerk, name="Inactive Ltd")
        Supplier.objects.filter(pk=approved.pk).update(status=SupplierStatus.APPROVED)
        Supplier.objects.filter(pk=rejected.pk).update(status=SupplierStatus.REJECTED)
        Supplier.objects.filter(pk=inactive.pk).update(is_active=False)

        body = http.get("/api/v1/sync/bundle").json()

        assert body["suppliers"] == [
            {"id": approved.pk, "name": "Approved Ltd", "status": "APPROVED"},
            {"id": pending.pk, "name": "Pending Ltd", "status": "PENDING"},
        ]
        text = str(body["suppliers"])
        for secret in ("P051234567A", "KCB", "777", "123456"):
            assert secret not in text

    def test_vehicles_are_active_vehicles_and_generators_only(self, http, tenant):
        hilux = Asset.objects.create(
            organization=tenant,
            type=AssetType.VEHICLE,
            name="Hilux",
            tag="KDA 123A",
            cost="1000000",
            purchase_terms="secret terms",
        )
        gen = Asset.objects.create(
            organization=tenant, type=AssetType.GENERATOR, name="Gen", tag="G-1"
        )
        Asset.objects.create(organization=tenant, type=AssetType.TOOL, name="Drill")
        Asset.objects.create(
            organization=tenant,
            type=AssetType.VEHICLE,
            name="Sold truck",
            tag="KBB 1",
            status=AssetStatus.CLOSED,
            closed_on=timezone.localdate() - timedelta(days=1),
            closed_reason=AssetCloseReason.SOLD,
        )

        body = http.get("/api/v1/sync/bundle").json()

        assert body["vehicles"] == [
            {"id": gen.pk, "name": "Gen", "tag": "G-1", "type": "GENERATOR"},
            {"id": hilux.pk, "name": "Hilux", "tag": "KDA 123A", "type": "VEHICLE"},
        ]
        assert "secret terms" not in str(body["vehicles"])

    def test_nothing_from_another_tenant(self, http, tenant, other_organization):
        from network.suppliers import add_supplier

        with tenant_context(other_organization):
            add_supplier(actor=UserFactory(organization=other_organization), name="Theirs Ltd")
            Asset.objects.create(
                organization=other_organization,
                type=AssetType.VEHICLE,
                name="Theirs",
                tag="KZZ 9",
            )

        body = http.get("/api/v1/sync/bundle").json()

        assert body["suppliers"] == []
        assert body["vehicles"] == []

    def test_the_http_endpoint_accepts_a_supplier_then_a_gate_in(self, http, tenant, clerk):
        from receiving.models import GateIn

        yard = YardFactory(name="Sync yard")
        supplier_uuid = str(uuid4())
        response = http.post(
            "/api/v1/sync/submissions",
            {
                "submissions": [
                    {
                        "client_uuid": supplier_uuid,
                        "operation": "SUPPLIER",
                        "payload": {"name": "Kenya Cable Ltd", "client_uuid": supplier_uuid},
                    },
                    {
                        "client_uuid": str(uuid4()),
                        "operation": "GATE_IN",
                        "payload": gate_in_body(yard, supplier_client_uuid=supplier_uuid),
                    },
                ]
            },
            format="json",
        )

        assert response.status_code == 200
        assert response.json()["applied"] == 2
        assert GateIn.objects.get().supplier.name == "Kenya Cable Ltd"


def test_the_submission_table_records_the_operation(tenant, clerk):
    send(tenant, clerk, "SUPPLIER", supplier_body())
    assert SyncSubmission.objects.get().operation == "SUPPLIER"
