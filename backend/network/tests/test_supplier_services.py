"""T17.3/T17.4 — supplier approval and services (§4.20.3, R15)."""

import pytest
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals import engine
from approvals.models import ApprovalRequest, ApprovalRequestStatus
from commercials.finance import FinanceNotDecidable, FinanceSelfApproval
from core.exceptions import PermissionDeniedError
from core.models import AuditLog
from core.rls import rls_bypass
from core.tenancy import tenant_context
from locations.factories import YardFactory
from network import suppliers
from network.models import SupplierStatus
from notifications.matrix import Event
from notifications.models import NotificationEvent
from receiving.models import GateIn, GateInSource

FULL = {"kra_pin": "P051234567A", "bank_name": "KCB", "account_number": "123"}


@pytest.fixture
def registrar(tenant):
    return UserFactory(organization=tenant)


@pytest.fixture
def finance_user(tenant):
    role = RoleFactory(name="Finance", codenames=[PERM.FINANCE_APPROVE])
    user = UserFactory(organization=tenant)
    UserRoleFactory(user=user, role=role)
    return user


def add(registrar, name="Kenya Cable Ltd", **kw):
    return suppliers.add_supplier(actor=registrar, name=name, **kw)


@pytest.mark.django_db
class TestRouting:
    def test_one_finance_level_no_manager(self, tenant, registrar):
        supplier = add(registrar)
        levels = engine.required_levels(supplier)
        assert [(lv.level, lv.permission, lv.user) for lv in levels] == [
            (1, "finance.approve", None)
        ]
        req = ApprovalRequest.objects.get(document_type="network.Supplier")
        assert req.document_id == str(supplier.pk)
        assert req.due_at is None
        assert supplier.status == SupplierStatus.PENDING

    def test_registrar_cannot_approve_own_entry(self, tenant, registrar):
        role = RoleFactory(name="Fin2", codenames=[PERM.FINANCE_APPROVE])
        UserRoleFactory(user=registrar, role=role)
        supplier = add(registrar, **FULL)
        req = ApprovalRequest.objects.get(document_id=str(supplier.pk))
        assert engine.can_approve(registrar, req, document=supplier) == (False, "self")
        with pytest.raises(FinanceSelfApproval):
            suppliers.decide_supplier(supplier, actor=registrar, approved=True)

    def test_holders_see_it_pending(self, tenant, registrar, finance_user):
        add(registrar)
        from approvals.addressing import open_requests_addressed_to

        mine = open_requests_addressed_to(finance_user, ApprovalRequest.objects.all())
        assert mine.filter(document_type="network.Supplier").count() == 1
        none = open_requests_addressed_to(registrar, ApprovalRequest.objects.all())
        assert none.count() == 0


@pytest.mark.django_db
class TestDecide:
    def test_approve(self, tenant, registrar, finance_user):
        supplier = add(registrar, **FULL)
        suppliers.decide_supplier(supplier, actor=finance_user, approved=True)
        supplier.refresh_from_db()
        assert supplier.status == SupplierStatus.APPROVED
        assert supplier.decided_by == finance_user
        assert supplier.is_usable
        req = ApprovalRequest.objects.get(document_id=str(supplier.pk))
        assert req.status == ApprovalRequestStatus.APPROVED

    def test_approval_needs_pin_and_payment_route(self, tenant, registrar, finance_user):
        supplier = add(registrar)
        with pytest.raises(suppliers.SupplierPinRequired):
            suppliers.decide_supplier(supplier, actor=finance_user, approved=True)
        suppliers.update_supplier(supplier, actor=registrar, changes={"kra_pin": "P1"})
        with pytest.raises(suppliers.SupplierPinRequired):
            suppliers.decide_supplier(supplier, actor=finance_user, approved=True)
        suppliers.update_supplier(
            supplier, actor=registrar, changes={"mpesa_type": "TILL", "mpesa_number": "5"}
        )
        suppliers.decide_supplier(supplier, actor=finance_user, approved=True)

    def test_reject_needs_reason_then_resubmit(self, tenant, registrar, finance_user):
        supplier = add(registrar, **FULL)
        with pytest.raises(Exception, match="reason"):
            suppliers.decide_supplier(supplier, actor=finance_user, approved=False)
        suppliers.decide_supplier(supplier, actor=finance_user, approved=False, reason="Bad PIN")
        supplier.refresh_from_db()
        assert supplier.status == SupplierStatus.REJECTED
        assert supplier.decision_reason == "Bad PIN"

        with pytest.raises(PermissionDeniedError):
            suppliers.resubmit(supplier, actor=finance_user)
        suppliers.resubmit(supplier, actor=registrar)
        supplier.refresh_from_db()
        assert supplier.status == SupplierStatus.PENDING
        assert supplier.decided_by is None
        assert (
            ApprovalRequest.objects.filter(
                document_id=str(supplier.pk), status=ApprovalRequestStatus.PENDING
            ).count()
            == 1
        )
        suppliers.decide_supplier(supplier, actor=finance_user, approved=True)

    def test_only_pending_is_decidable(self, tenant, registrar, finance_user):
        supplier = add(registrar, **FULL)
        suppliers.decide_supplier(supplier, actor=finance_user, approved=True)
        with pytest.raises(FinanceNotDecidable):
            suppliers.decide_supplier(supplier, actor=finance_user, approved=True)
        with pytest.raises(FinanceNotDecidable):
            suppliers.resubmit(supplier, actor=registrar)

    def test_non_holder_is_refused(self, tenant, registrar):
        supplier = add(registrar, **FULL)
        with pytest.raises(engine.NotAnApprover):
            outsider = UserFactory(organization=tenant)
            suppliers.decide_supplier(supplier, actor=outsider, approved=True)


@pytest.mark.django_db
class TestSensitiveEdit:
    def test_stays_approved_audited_and_finance_told(self, tenant, registrar, finance_user):
        supplier = add(registrar, **FULL)
        suppliers.decide_supplier(supplier, actor=finance_user, approved=True)
        NotificationEvent.objects.all().delete()

        suppliers.update_supplier(
            supplier, actor=registrar, changes={"account_number": "999", "phone": "0700"}
        )
        supplier.refresh_from_db()
        assert supplier.status == SupplierStatus.APPROVED
        assert supplier.account_number == "999"
        log = AuditLog.objects.filter(
            target_id=str(supplier.pk), note__startswith="Payment details changed"
        ).get()
        assert log.before == {"account_number": "123", "phone": ""} | {}
        assert log.after == {"account_number": "999", "phone": "0700"}
        event = NotificationEvent.objects.get(event_key=Event.SUPPLIER_DETAILS_CHANGED)
        assert event.payload["changed"] == ["account_number"]
        assert not ApprovalRequest.objects.filter(
            document_id=str(supplier.pk), status=ApprovalRequestStatus.PENDING
        ).exists()

    def test_non_sensitive_edit_does_not_notify(self, tenant, registrar, finance_user):
        supplier = add(registrar, **FULL)
        suppliers.decide_supplier(supplier, actor=finance_user, approved=True)
        suppliers.update_supplier(supplier, actor=registrar, changes={"phone": "0711"})
        assert not NotificationEvent.objects.filter(
            event_key=Event.SUPPLIER_DETAILS_CHANGED
        ).exists()

    def test_pending_edit_does_not_notify(self, tenant, registrar):
        supplier = add(registrar)
        suppliers.update_supplier(supplier, actor=registrar, changes={"kra_pin": "P9"})
        assert not NotificationEvent.objects.filter(
            event_key=Event.SUPPLIER_DETAILS_CHANGED
        ).exists()


@pytest.mark.django_db
class TestDuplicates:
    def test_name_duplicate_names_existing(self, tenant, registrar):
        first = add(registrar, "Kenya Cable Ltd")
        with pytest.raises(suppliers.SupplierNameDuplicate) as exc:
            add(registrar, "  kenya   CABLE ltd")
        assert exc.value.details["existing"] == {
            "id": first.pk,
            "name": "Kenya Cable Ltd",
            "status": "PENDING",
        }

    def test_pin_duplicate_names_existing(self, tenant, registrar):
        first = add(registrar, "A", kra_pin="P 051 234 567 A")
        with pytest.raises(suppliers.SupplierPinDuplicate) as exc:
            add(registrar, "B", kra_pin="p051234567a")
        assert exc.value.details["existing"]["id"] == first.pk

    def test_edit_into_a_duplicate_is_refused_but_own_values_pass(self, tenant, registrar):
        add(registrar, "A", kra_pin="P1")
        second = add(registrar, "B")
        with pytest.raises(suppliers.SupplierPinDuplicate):
            suppliers.update_supplier(second, actor=registrar, changes={"kra_pin": "p1"})
        suppliers.update_supplier(second, actor=registrar, changes={"name": "B"})

    def test_name_required(self, tenant, registrar):
        with pytest.raises(suppliers.SupplierNameRequired):
            add(registrar, "  ")

    def test_client_uuid_replays(self, tenant, registrar):
        import uuid

        key = uuid.uuid4()
        first = add(registrar, client_uuid=key)
        again = add(registrar, client_uuid=key)
        assert first.pk == again.pk


@pytest.mark.django_db
class TestLinkHistory:
    def _gate_in(self, org, yard, name):
        return GateIn.objects.create(
            organization=org,
            to_location=yard,
            received_at=timezone.now(),
            source_type=GateInSource.PURCHASE,
            supplier_name=name,
        )

    def test_links_by_name_key_and_is_idempotent(self, tenant, registrar):
        yard = YardFactory(organization=tenant)
        supplier = add(registrar, "Kenya Cable Ltd")
        other = add(registrar, "Other Co")
        match = self._gate_in(tenant, yard, "KENYA  cable ltd")
        miss = self._gate_in(tenant, yard, "Someone Else")
        taken = self._gate_in(tenant, yard, "Kenya Cable Ltd")
        GateIn.objects.filter(pk=taken.pk).update(supplier=other)

        assert suppliers.link_history(supplier, actor=registrar) == 1
        assert suppliers.link_history(supplier, actor=registrar) == 0
        match.refresh_from_db()
        miss.refresh_from_db()
        taken.refresh_from_db()
        assert match.supplier_id == supplier.pk
        assert match.supplier_name == "KENYA  cable ltd"
        assert miss.supplier_id is None
        assert taken.supplier_id == other.pk

    def test_tenant_bounded(self, tenant, other_organization, registrar):
        supplier = add(registrar, "Kenya Cable Ltd")
        with tenant_context(other_organization):
            other_yard = YardFactory(organization=other_organization)
            foreign = self._gate_in(other_organization, other_yard, "Kenya Cable Ltd")
        assert suppliers.link_history(supplier, actor=registrar) == 0
        with rls_bypass():
            foreign.refresh_from_db()
        assert foreign.supplier_id is None


@pytest.mark.django_db
class TestPayable:
    def test_only_approved_and_active(self, tenant, registrar, finance_user):
        supplier = add(registrar, **FULL)
        with pytest.raises(suppliers.SupplierNotApproved) as exc:
            suppliers.assert_payable(supplier)
        assert exc.value.status_code == 409
        suppliers.decide_supplier(supplier, actor=finance_user, approved=True)
        supplier.refresh_from_db()
        suppliers.assert_payable(supplier)
        suppliers.set_active(supplier, actor=finance_user, active=False)
        with pytest.raises(suppliers.SupplierNotApproved):
            suppliers.assert_payable(supplier)
        suppliers.set_active(supplier, actor=finance_user, active=True)
        suppliers.assert_payable(supplier)

    def test_rejected_is_not_payable(self, tenant, registrar, finance_user):
        supplier = add(registrar, **FULL)
        suppliers.decide_supplier(supplier, actor=finance_user, approved=False, reason="no")
        supplier.refresh_from_db()
        with pytest.raises(suppliers.SupplierNotApproved):
            suppliers.assert_payable(supplier)
