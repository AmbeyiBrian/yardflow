"""T17.1 — the supplier register model and the gate-in link (§4.20.2, R15)."""

import importlib

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import UserFactory
from core.rls import rls_bypass
from core.tenancy import tenant_context
from locations.factories import YardFactory
from network.models import Supplier, SupplierStatus
from receiving.models import GateIn, GateInSource


def make(org, name="Kenya Cable Ltd", **kwargs):
    kwargs.setdefault("registered_by", UserFactory(organization=org))
    return Supplier.objects.create(organization=org, name=name, **kwargs)


def make_gate_in(org, yard, **kwargs):
    return GateIn.objects.create(
        organization=org,
        to_location=yard,
        received_at=timezone.now(),
        source_type=GateInSource.PURCHASE,
        **kwargs,
    )


@pytest.mark.django_db
class TestTheRegister:
    def test_a_new_supplier_is_pending_and_active(self, tenant):
        supplier = make(tenant)
        assert supplier.status == SupplierStatus.PENDING
        assert supplier.is_active
        assert not supplier.is_usable

    def test_keys_are_derived_on_save(self, tenant):
        supplier = make(tenant, name="  Kenya   CABLE ltd ", kra_pin="p 051 234 567 a")
        assert supplier.name_key == "kenya cable ltd"
        assert supplier.kra_pin_key == "P051234567A"

    def test_keys_follow_update_fields_saves(self, tenant):
        supplier = make(tenant, kra_pin="P1")
        supplier.name = "Other   Name"
        supplier.kra_pin = "p 2"
        supplier.save(update_fields=["name", "kra_pin"])
        supplier.refresh_from_db()
        assert (supplier.name_key, supplier.kra_pin_key) == ("other name", "P2")

    def test_a_name_is_unique_by_key_within_a_tenant(self, tenant):
        make(tenant, name="Kenya Cable Ltd")
        with pytest.raises(IntegrityError), transaction.atomic():
            make(tenant, name="kenya  cable LTD")

    def test_a_pin_is_unique_by_key_within_a_tenant(self, tenant):
        make(tenant, name="A", kra_pin="P051234567A")
        with pytest.raises(IntegrityError), transaction.atomic():
            make(tenant, name="B", kra_pin="p051 234 567a")

    def test_blank_pins_do_not_collide(self, tenant):
        make(tenant, name="A")
        make(tenant, name="B")
        assert Supplier.objects.count() == 2

    def test_the_same_name_and_pin_may_exist_in_two_tenants(
        self, organization, other_organization
    ):
        for org in (organization, other_organization):
            with tenant_context(org):
                make(org, kra_pin="P051234567A")
        with rls_bypass():
            assert Supplier.all_objects.count() == 2

    def test_a_client_uuid_replays_to_one_row(self, tenant):
        import uuid

        key = uuid.uuid4()
        make(tenant, name="A", client_uuid=key)
        with pytest.raises(IntegrityError), transaction.atomic():
            make(tenant, name="B", client_uuid=key)

    def test_it_is_never_deleted(self, tenant):
        supplier = make(tenant)
        with pytest.raises(ValidationError):
            supplier.delete()
        assert Supplier.objects.count() == 1


@pytest.mark.django_db
class TestGateInLink:
    def test_the_supplier_is_optional_and_old_rows_are_intact(self, tenant):
        yard = YardFactory(name="Main yard")
        gate_in = make_gate_in(tenant, yard, supplier_name="Cable Supplies Ltd")
        gate_in.refresh_from_db()
        assert gate_in.supplier is None
        assert gate_in.supplier_name == "Cable Supplies Ltd"

    def test_a_named_supplier_is_protected_from_deletion(self, tenant):
        from django.db.models import ProtectedError

        yard = YardFactory(name="Main yard")
        supplier = make(tenant)
        make_gate_in(tenant, yard, supplier=supplier, supplier_name=supplier.name)
        with pytest.raises(ProtectedError):
            Supplier.objects.filter(pk=supplier.pk).delete()


@pytest.mark.django_db
class TestHistoryLinkMigration:
    @staticmethod
    def run():
        module = importlib.import_module("receiving.migrations.0010_link_gate_in_suppliers")
        module.link(apps, None)

    def test_it_is_a_no_op_on_an_empty_register(self, tenant):
        yard = YardFactory(name="Main yard")
        gate_in = make_gate_in(tenant, yard, supplier_name="Kenya Cable Ltd")
        self.run()
        gate_in.refresh_from_db()
        assert gate_in.supplier is None
        assert Supplier.objects.count() == 0

    def test_it_links_by_name_key_and_leaves_the_rest(self, tenant):
        yard = YardFactory(name="Main yard")
        supplier = make(tenant, name="Kenya Cable Ltd")
        match = make_gate_in(tenant, yard, supplier_name="  kenya CABLE  ltd")
        near_miss = make_gate_in(tenant, yard, supplier_name="Kenya Cables Ltd")
        blank = make_gate_in(tenant, yard)
        other = make(tenant, name="Other Co")
        already = make_gate_in(tenant, yard, supplier_name="Kenya Cable Ltd", supplier=other)

        self.run()

        for row in (match, near_miss, blank, already):
            row.refresh_from_db()
        assert match.supplier == supplier
        assert near_miss.supplier is None
        assert blank.supplier is None
        assert already.supplier == other  # never overwritten
        assert Supplier.objects.count() == 2  # never creates

    def test_it_does_not_cross_tenants(self, organization, other_organization):
        with tenant_context(organization):
            make(organization, name="Kenya Cable Ltd")
        with tenant_context(other_organization):
            yard = YardFactory(name="Their yard")
            theirs = make_gate_in(other_organization, yard, supplier_name="Kenya Cable Ltd")

        self.run()

        with tenant_context(other_organization):
            theirs.refresh_from_db()
            assert theirs.supplier is None
