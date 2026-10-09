"""T17.7 — supplier endpoints and documents (§4.20.6, §4.20.7; R15)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from commercials.tests.api_helpers import PASSWORD, Api, error_code, results
from core.models import Attachment, AuditLog
from network import suppliers
from network.models import Supplier, SupplierStatus

SUPPLIER = "network.Supplier"
FULL = {"kra_pin": "P051234567A", "bank_name": "KCB", "account_number": "123"}
PRIVATE = ("kra_pin", "bank_name", "account_number", "mpesa_number", "email", "address")


@pytest.fixture(autouse=True)
def _domain(settings):
    settings.TENANT_BASE_DOMAIN = "localhost"


def person(tenant, name, *codenames):
    user = UserFactory(organization=tenant, full_name=name, password=PASSWORD)
    if codenames:
        UserRoleFactory(user=user, role=RoleFactory(codenames=list(codenames)))
    return user


def doc(name="pin.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 pretend", content_type="application/pdf")


@pytest.fixture
def reg(tenant):
    return person(tenant, "Rita Registrar")


@pytest.fixture
def other(tenant):
    return person(tenant, "Olive Other")


@pytest.fixture
def fin(tenant):
    return person(tenant, "Fiona Finance", PERM.FINANCE_APPROVE)


def make(reg, name="Kenya Cable Ltd", **kw):
    return suppliers.add_supplier(actor=reg, name=name, **{**FULL, **kw})


@pytest.mark.django_db
class TestListAndCreate:
    def test_create_returns_pending_with_registrar(self, tenant, client, reg):
        response = Api(client, reg).post(
            "suppliers", {"name": "  Acme Ltd ", "phone": "0700", **FULL}
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["name"] == "Acme Ltd"
        assert body["status"] == "PENDING"
        assert body["is_active"] is True
        assert body["registered_by"] == reg.pk
        assert body["registered_by_name"] == "Rita Registrar"
        assert body["kra_pin"] == FULL["kra_pin"]

    def test_status_cannot_be_posted(self, tenant, client, reg):
        body = Api(client, reg).post(
            "suppliers", {"name": "Sneaky", "status": "APPROVED", "is_active": False}
        ).json()
        assert body["status"] == "PENDING"
        assert body["is_active"] is True

    def test_name_required(self, tenant, client, reg):
        assert Api(client, reg).post("suppliers", {"name": ""}).status_code == 400

    def test_name_duplicate_names_existing(self, tenant, client, reg, other):
        existing = make(reg)
        response = Api(client, other).post("suppliers", {"name": " kenya  CABLE ltd"})
        assert response.status_code == 409
        assert error_code(response) == "SUPPLIER_NAME_DUPLICATE"
        found = response.json()["error"]["details"]["existing"]
        assert found["id"] == existing.pk
        assert found["name"] == "Kenya Cable Ltd"

    def test_pin_duplicate_names_existing(self, tenant, client, reg, other):
        existing = make(reg)
        response = Api(client, other).post(
            "suppliers", {"name": "Another", "kra_pin": "p051 234567a"}
        )
        assert response.status_code == 409
        assert error_code(response) == "SUPPLIER_PIN_DUPLICATE"
        assert response.json()["error"]["details"]["existing"]["id"] == existing.pk

    def test_client_uuid_replays(self, tenant, client, reg):
        api = Api(client, reg)
        uid = "0b0b0b0b-0b0b-4b0b-8b0b-0b0b0b0b0b0b"
        first = api.post("suppliers", {"name": "Offline Co", "client_uuid": uid}).json()
        second = api.post("suppliers", {"name": "Offline Co", "client_uuid": uid}).json()
        assert first["id"] == second["id"]
        assert Supplier.objects.filter(name="Offline Co").count() == 1

    def test_filters_and_search(self, tenant, client, reg, other, fin):
        a = make(reg, "Alpha Cables", kra_pin="A1", contact_name="Zed")
        b = make(reg, "Beta Steel", kra_pin="B1")
        c = make(reg, "Gamma Paint", kra_pin="C1")
        suppliers.decide_supplier(a, actor=fin, approved=True)
        suppliers.decide_supplier(b, actor=fin, approved=False, reason="no")
        suppliers.set_active(c, actor=fin, active=False)
        api = Api(client, other)

        def names(**params):
            return sorted(r["name"] for r in results(api.get("suppliers", **params)))

        assert names() == ["Alpha Cables", "Beta Steel", "Gamma Paint"]
        assert names(status="APPROVED") == ["Alpha Cables"]
        assert names(status="REJECTED") == ["Beta Steel"]
        assert names(is_active="false") == ["Gamma Paint"]
        assert names(is_active="true") == ["Alpha Cables", "Beta Steel"]
        assert names(payable="true") == ["Alpha Cables"]
        assert names(search="alph") == ["Alpha Cables"]
        assert names(search="zed") == ["Alpha Cables"]

    def test_payable_excludes_deactivated_approved(self, tenant, client, reg, fin):
        a = make(reg)
        suppliers.decide_supplier(a, actor=fin, approved=True)
        suppliers.set_active(a, actor=fin, active=False)
        assert results(Api(client, reg).get("suppliers", payable="true")) == []

    def test_detail(self, tenant, client, reg):
        supplier = make(reg)
        assert Api(client, reg).get(f"suppliers/{supplier.pk}").json()["id"] == supplier.pk

    def test_delete_not_offered(self, tenant, client, reg, fin):
        supplier = make(reg)
        assert Api(client, fin).delete(f"suppliers/{supplier.pk}").status_code == 405


@pytest.mark.django_db
class TestVisibility:
    def test_member_gets_no_payment_data(self, tenant, client, reg, other):
        make(reg, mpesa_type="PAYBILL", mpesa_number="522522", phone="0700")
        row = results(Api(client, other).get("suppliers"))[0]
        for name in PRIVATE:
            assert name not in row
        assert row["name"] == "Kenya Cable Ltd"
        assert row["phone"] == "0700"
        assert row["status"] == "PENDING"
        assert "522522" not in Api(client, other).get("suppliers").content.decode()

    def test_registrar_and_finance_see_it(self, tenant, client, reg, fin):
        supplier = make(reg)
        for user in (reg, fin):
            body = Api(client, user).get(f"suppliers/{supplier.pk}").json()
            assert body["kra_pin"] == FULL["kra_pin"]
            assert body["account_number"] == "123"

    def test_member_cannot_search_by_pin(self, tenant, client, reg, other, fin):
        make(reg)
        assert results(Api(client, other).get("suppliers", search="P051234567A")) == []
        assert len(results(Api(client, fin).get("suppliers", search="p051234567a"))) == 1


@pytest.mark.django_db
class TestPatch:
    def test_registrar_edits_while_pending(self, tenant, client, reg):
        supplier = make(reg)
        response = Api(client, reg).patch(f"suppliers/{supplier.pk}", {"phone": "0711"})
        assert response.status_code == 200, response.content
        assert response.json()["phone"] == "0711"

    def test_other_member_refused(self, tenant, client, reg, other):
        supplier = make(reg)
        response = Api(client, other).patch(f"suppliers/{supplier.pk}", {"phone": "1"})
        assert response.status_code == 403
        supplier.refresh_from_db()
        assert supplier.phone == ""

    def test_registrar_locked_out_once_approved(self, tenant, client, reg, fin):
        supplier = make(reg)
        suppliers.decide_supplier(supplier, actor=fin, approved=True)
        response = Api(client, reg).patch(f"suppliers/{supplier.pk}", {"phone": "1"})
        assert response.status_code == 403

    def test_finance_edits_approved_and_stays_approved(self, tenant, client, reg, fin):
        supplier = make(reg)
        suppliers.decide_supplier(supplier, actor=fin, approved=True)
        response = Api(client, fin).patch(f"suppliers/{supplier.pk}", {"account_number": "999"})
        assert response.status_code == 200, response.content
        assert response.json()["status"] == "APPROVED"
        assert AuditLog.objects.filter(note__contains="Payment details changed").exists()

    def test_registrar_edits_while_rejected(self, tenant, client, reg, fin):
        supplier = make(reg)
        suppliers.decide_supplier(supplier, actor=fin, approved=False, reason="Wrong PIN")
        body = Api(client, reg).patch(f"suppliers/{supplier.pk}", {"kra_pin": "P09"}).json()
        assert body["kra_pin"] == "P09"
        assert body["decision_reason"] == "Wrong PIN"

    def test_patch_into_duplicate(self, tenant, client, reg):
        make(reg, "One", kra_pin="P1")
        two = make(reg, "Two", kra_pin="P2")
        response = Api(client, reg).patch(f"suppliers/{two.pk}", {"kra_pin": "p1"})
        assert response.status_code == 409
        assert response.json()["error"]["details"]["existing"]["name"] == "One"

    def test_status_cannot_be_patched(self, tenant, client, reg):
        supplier = make(reg)
        Api(client, reg).patch(f"suppliers/{supplier.pk}", {"status": "APPROVED"})
        supplier.refresh_from_db()
        assert supplier.status == SupplierStatus.PENDING


@pytest.mark.django_db
class TestDecisions:
    def test_finance_approves(self, tenant, client, reg, fin):
        supplier = make(reg)
        response = Api(client, fin).post(f"suppliers/{supplier.pk}/decide", {"approved": True})
        assert response.status_code == 200, response.content
        assert response.json()["status"] == "APPROVED"

    def test_member_cannot_decide(self, tenant, client, reg, other):
        supplier = make(reg)
        response = Api(client, other).post(f"suppliers/{supplier.pk}/decide", {"approved": True})
        assert response.status_code == 403

    def test_registrar_cannot_self_approve(self, tenant, client, reg):
        role = RoleFactory(name="Fin2", codenames=[PERM.FINANCE_APPROVE])
        UserRoleFactory(user=reg, role=role)
        supplier = make(reg)
        response = Api(client, reg).post(f"suppliers/{supplier.pk}/decide", {"approved": True})
        assert response.status_code in (403, 409)
        supplier.refresh_from_db()
        assert supplier.status == SupplierStatus.PENDING

    def test_reject_needs_reason_then_resubmit(self, tenant, client, reg, fin):
        supplier = make(reg)
        fin_api, reg_api = Api(client, fin), Api(client, reg)
        assert (
            fin_api.post(f"suppliers/{supplier.pk}/decide", {"approved": False}).status_code
            == 400
        )
        rejected = fin_api.post(
            f"suppliers/{supplier.pk}/decide", {"approved": False, "reason": "Blurry PIN"}
        ).json()
        assert rejected["status"] == "REJECTED"
        assert rejected["decision_reason"] == "Blurry PIN"
        again = reg_api.post(f"suppliers/{supplier.pk}/resubmit")
        assert again.status_code == 200, again.content
        assert again.json()["status"] == "PENDING"
        assert again.json()["decision_reason"] == ""

    def test_only_registrar_resubmits(self, tenant, client, reg, fin, other):
        supplier = make(reg)
        suppliers.decide_supplier(supplier, actor=fin, approved=False, reason="x")
        assert Api(client, other).post(f"suppliers/{supplier.pk}/resubmit").status_code == 403

    def test_cannot_decide_twice(self, tenant, client, reg, fin):
        supplier = make(reg)
        api = Api(client, fin)
        api.post(f"suppliers/{supplier.pk}/decide", {"approved": True})
        assert api.post(f"suppliers/{supplier.pk}/decide", {"approved": True}).status_code == 409

    def test_approval_needs_pin(self, tenant, client, reg, fin):
        supplier = make(reg, "No Pin Co", kra_pin="", bank_name="", account_number="")
        response = Api(client, fin).post(f"suppliers/{supplier.pk}/decide", {"approved": True})
        assert error_code(response) == "SUPPLIER_PIN_REQUIRED"

    def test_deactivate_reactivate(self, tenant, client, reg, fin, other):
        supplier = make(reg)
        suppliers.decide_supplier(supplier, actor=fin, approved=True)
        assert Api(client, other).post(f"suppliers/{supplier.pk}/deactivate").status_code == 403
        assert Api(client, reg).post(f"suppliers/{supplier.pk}/reactivate").status_code == 403
        off = Api(client, fin).post(f"suppliers/{supplier.pk}/deactivate").json()
        assert off["is_active"] is False
        on = Api(client, fin).post(f"suppliers/{supplier.pk}/reactivate").json()
        assert on["is_active"] is True
        assert on["status"] == "APPROVED"

    def test_link_history(self, tenant, client, reg, fin, other):
        supplier = make(reg)
        assert Api(client, other).post(f"suppliers/{supplier.pk}/link-history").status_code == 403
        response = Api(client, fin).post(f"suppliers/{supplier.pk}/link-history")
        assert response.status_code == 200
        assert response.json() == {"linked": 0}


@pytest.mark.django_db
class TestDocuments:
    def upload(self, api, supplier, **extra):
        return api.upload(
            {"target_type": SUPPLIER, "target_id": str(supplier.pk), "file": doc(), **extra}
        )

    def test_registrar_attaches_while_pending_and_lists(self, tenant, client, reg):
        supplier = make(reg)
        api = Api(client, reg)
        response = self.upload(api, supplier, caption="KRA PIN certificate")
        assert response.status_code == 201, response.content
        listed = results(api.get("attachments", target_type=SUPPLIER, target_id=supplier.pk))
        assert [a["caption"] for a in listed] == ["KRA PIN certificate"]

    def test_other_member_cannot_attach_or_see(self, tenant, client, reg, other):
        supplier = make(reg)
        self.upload(Api(client, reg), supplier)
        theirs = Api(client, other)
        assert self.upload(theirs, supplier).status_code == 403
        assert results(theirs.get("attachments", target_type=SUPPLIER, target_id=supplier.pk)) == []

    def test_finance_sees_and_attaches_always(self, tenant, client, reg, fin):
        supplier = make(reg)
        self.upload(Api(client, reg), supplier)
        suppliers.decide_supplier(supplier, actor=fin, approved=True)
        api = Api(client, fin)
        assert self.upload(api, supplier).status_code == 201
        listed = results(api.get("attachments", target_type=SUPPLIER, target_id=supplier.pk))
        assert len(listed) == 2

    def test_registrar_locked_once_approved(self, tenant, client, reg, fin):
        supplier = make(reg)
        first = self.upload(Api(client, reg), supplier).json()
        suppliers.decide_supplier(supplier, actor=fin, approved=True)
        api = Api(client, reg)
        locked = self.upload(api, supplier)
        assert locked.status_code == 409
        assert error_code(locked) == "ATTACHMENT_LOCKED"
        assert api.delete(f"attachments/{first['id']}").status_code == 409
        assert Attachment.objects.filter(pk=first["id"]).exists()

    def test_registrar_can_attach_when_rejected(self, tenant, client, reg, fin):
        supplier = make(reg)
        suppliers.decide_supplier(supplier, actor=fin, approved=False, reason="Need PIN scan")
        assert self.upload(Api(client, reg), supplier).status_code == 201

    def test_registrar_can_remove_own_while_pending(self, tenant, client, reg):
        supplier = make(reg)
        api = Api(client, reg)
        made = self.upload(api, supplier).json()
        assert api.delete(f"attachments/{made['id']}").status_code == 204

    def test_bad_type_refused(self, tenant, client, reg):
        supplier = make(reg)
        bad = SimpleUploadedFile("x.exe", b"MZ", content_type="application/x-msdownload")
        response = Api(client, reg).upload(
            {"target_type": SUPPLIER, "target_id": str(supplier.pk), "file": bad}
        )
        assert response.status_code == 400

    def test_targets_lists_supplier(self, tenant, client, reg, other):
        body = Api(client, other).get("attachment-targets").json()
        assert body["targets"][SUPPLIER] is True


@pytest.mark.django_db
class TestIsolation:
    def test_other_tenant_is_404(self, tenant, other_organization, client, reg):
        from core.tenancy import tenant_context

        with tenant_context(other_organization):
            foreign_user = UserFactory(organization=other_organization)
            foreign = suppliers.add_supplier(actor=foreign_user, name="Rival Supplies")
        api = Api(client, reg)
        assert [r["name"] for r in results(api.get("suppliers"))] == []
        assert api.get(f"suppliers/{foreign.pk}").status_code == 404
        assert api.patch(f"suppliers/{foreign.pk}", {"phone": "1"}).status_code == 404
        assert api.post(f"suppliers/{foreign.pk}/decide", {"approved": True}).status_code in (
            403,
            404,
        )
        assert api.post(f"suppliers/{foreign.pk}/resubmit").status_code == 404
        upload = api.upload(
            {"target_type": SUPPLIER, "target_id": str(foreign.pk), "file": doc()}
        )
        assert upload.status_code == 404

    def test_same_name_allowed_across_tenants(self, tenant, other_organization, client, reg):
        from core.tenancy import tenant_context

        with tenant_context(other_organization):
            foreign_user = UserFactory(organization=other_organization)
            suppliers.add_supplier(actor=foreign_user, name="Kenya Cable Ltd", **FULL)
        response = Api(client, reg).post("suppliers", {"name": "Kenya Cable Ltd", **FULL})
        assert response.status_code == 201, response.content
