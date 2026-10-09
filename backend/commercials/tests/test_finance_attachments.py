"""T15.4 — attachments on money entries (§4.17.6, §4.17.7; R1, R3, R6).

The owner rule: the recorder (or a casual's registrar) adds photos while the entry
can still change, nobody touches them once it is approved, and a casual's ID
photo is for Finance and whoever registered them.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from commercials import finance
from commercials.models import Casual, ExpenseCategory
from commercials.tests.api_helpers import PASSWORD, Api, error_code, results
from commercials.tests.finance_helpers import approve_through
from core.models import Attachment
from network.factories import ProjectFactory, SiteFactory

D = Decimal
EXPENSE = "commercials.ProjectExpense"
CASUAL = "commercials.Casual"


@pytest.fixture(autouse=True)
def _domain(settings):
    settings.TENANT_BASE_DOMAIN = "localhost"


def person(tenant, name, *codenames):
    user = UserFactory(organization=tenant, full_name=name, password=PASSWORD)
    if codenames:
        UserRoleFactory(user=user, role=RoleFactory(codenames=list(codenames)))
    return user


def photo(name="receipt.jpg"):
    return SimpleUploadedFile(name, b"\xff\xd8\xff\xd9 pretend jpeg", content_type="image/jpeg")


@pytest.fixture
def pm(tenant):
    return person(tenant, "Pippa Manager")


@pytest.fixture
def tech(tenant):
    return person(tenant, "Tom Technician")


@pytest.fixture
def other(tenant):
    return person(tenant, "Olive Other")


@pytest.fixture
def fin(tenant):
    return person(tenant, "Fiona Finance", PERM.FINANCE_APPROVE)


@pytest.fixture
def expense(tenant, tech, pm, fin):
    project = ProjectFactory(
        reference="WO-9901",
        po_number="PO-990",
        manager=pm,
        contract_value=D("100000.00"),
        cost_budget=D("100000.00"),
    )
    site = SiteFactory(name="Ruiru")
    project.sites.add(
        site,
        through_defaults={"organization_id": project.organization_id},
    )
    category = ExpenseCategory.objects.create(organization=tenant, name="Misc")
    return finance.record_expense(
        actor=tech,
        category=category,
        amount=D("1000"),
        incurred_on=date(2026, 10, 5),
        site=site,
    )


@pytest.fixture
def casual(tenant, tech):
    return Casual.objects.create(name="Juma", id_number="12345678", registered_by=tech)


def upload(http, target_type, target_id, **extra):
    return http.upload(
        {"target_type": target_type, "target_id": str(target_id), "file": photo(), **extra}
    )


@pytest.mark.django_db
class TestCaptionAndIdempotency:
    def test_a_caption_is_kept_and_listed(self, client, tech, expense):
        http = Api(client, tech)

        response = upload(http, EXPENSE, expense.pk, caption="Fuel pump")

        assert response.status_code == 201, response.content
        assert response.json()["caption"] == "Fuel pump"
        listed = results(http.get("attachments", target_type=EXPENSE, target_id=expense.pk))
        assert [row["caption"] for row in listed] == ["Fuel pump"]

    def test_the_caption_is_capped(self, client, tech, expense):
        response = upload(Api(client, tech), EXPENSE, expense.pk, caption="x" * 61)

        assert response.status_code == 400

    def test_a_repeated_client_uuid_returns_the_attachment_it_made(self, client, tech, expense):
        http = Api(client, tech)
        key = str(uuid.uuid4())

        first = upload(http, EXPENSE, expense.pk, client_uuid=key)
        again = upload(http, EXPENSE, expense.pk, client_uuid=key)

        assert first.status_code == 201
        assert again.status_code == 200
        assert again.json()["id"] == first.json()["id"]
        assert Attachment.objects.filter(client_uuid=key).count() == 1

    def test_a_replay_still_lands_after_the_entry_was_approved(
        self, client, tenant, tech, pm, fin, expense
    ):
        """The response was lost, the entry moved on: the phone's retry is not an error."""
        http = Api(client, tech)
        key = str(uuid.uuid4())
        first = upload(http, EXPENSE, expense.pk, client_uuid=key)
        approve_through(expense, pm=pm, finance_user=fin)

        again = upload(http, EXPENSE, expense.pk, client_uuid=key)

        assert again.status_code == 200
        assert again.json()["id"] == first.json()["id"]

    def test_the_same_uuid_for_a_different_record_is_refused(self, client, tech, expense, casual):
        http = Api(client, tech)
        key = str(uuid.uuid4())
        upload(http, EXPENSE, expense.pk, client_uuid=key)

        response = upload(http, CASUAL, casual.pk, client_uuid=key)

        assert response.status_code == 400
        assert "client_uuid" in response.json()["error"]["field_errors"]

    def test_an_upload_without_either_still_works_as_before(self, client, tech, expense):
        response = upload(Api(client, tech), EXPENSE, expense.pk)

        assert response.status_code == 201
        assert response.json()["caption"] == ""
        assert response.json()["client_uuid"] is None


@pytest.mark.django_db
class TestTheOwnerRuleOnAnExpense:
    def test_the_recorder_attaches_while_it_is_pending(self, client, tech, expense):
        """Any member records, so no gate-in or closeout permission is needed."""
        assert upload(Api(client, tech), EXPENSE, expense.pk).status_code == 201

    def test_somebody_else_may_not(self, client, other, expense):
        response = upload(Api(client, other), EXPENSE, expense.pk)

        assert response.status_code == 403

    def test_the_pm_may_not_add_to_the_recorders_entry(self, client, pm, expense):
        assert upload(Api(client, pm), EXPENSE, expense.pk).status_code == 403

    def test_a_rejected_entry_may_still_gain_photos(self, client, tech, pm, expense):
        finance.decide(expense, actor=pm, approved=False, reason="No receipt")

        assert upload(Api(client, tech), EXPENSE, expense.pk).status_code == 201

    def test_pending_finance_may_still_gain_photos(self, client, tech, pm, expense):
        finance.decide(expense, actor=pm, approved=True)

        assert upload(Api(client, tech), EXPENSE, expense.pk).status_code == 201

    def test_nobody_adds_after_approval(self, client, tenant, tech, pm, fin, expense):
        approve_through(expense, pm=pm, finance_user=fin)

        for user in (tech, pm, fin):
            response = upload(Api(client, user), EXPENSE, expense.pk)
            assert response.status_code in (403, 409)
        locked = upload(Api(client, tech), EXPENSE, expense.pk)
        assert error_code(locked) == "ATTACHMENT_LOCKED"

    def test_nobody_adds_after_payment(self, client, tenant, tech, pm, fin, expense):
        approve_through(expense, pm=pm, finance_user=fin)
        finance.mark_paid(expense, actor=fin, reference="MPESA1")

        assert upload(Api(client, tech), EXPENSE, expense.pk).status_code == 409

    def test_nobody_removes_after_approval(self, client, tenant, tech, pm, fin, expense):
        http = Api(client, tech)
        made = upload(http, EXPENSE, expense.pk).json()
        approve_through(expense, pm=pm, finance_user=fin)

        response = http.delete(f"attachments/{made['id']}")

        assert response.status_code == 409
        assert error_code(response) == "ATTACHMENT_LOCKED"
        assert Attachment.objects.filter(pk=made["id"]).exists()

    def test_the_recorder_removes_their_own_while_pending(self, client, tech, expense):
        http = Api(client, tech)
        made = upload(http, EXPENSE, expense.pk).json()

        assert http.delete(f"attachments/{made['id']}").status_code == 204

    def test_evidence_state_follows_the_photos(self, client, tech, fin, expense):
        http = Api(client, tech)
        assert http.get(f"project-expenses/{expense.pk}").json()["evidence_state"] == "none"

        upload(http, EXPENSE, expense.pk)

        assert http.get(f"project-expenses/{expense.pk}").json()["evidence_state"] == "ok"

    def test_the_targets_endpoint_says_a_plain_member_can_attach(self, client, other):
        body = Api(client, other).get("attachment-targets").json()

        assert body["targets"][EXPENSE] is True
        assert body["targets"][CASUAL] is True


@pytest.mark.django_db
class TestACasualsIdPhoto:
    def test_the_registrar_attaches_and_reads_it(self, client, tech, casual):
        http = Api(client, tech)

        made = upload(http, CASUAL, casual.pk, caption="ID")

        assert made.status_code == 201, made.content
        assert len(results(http.get("attachments", target_type=CASUAL, target_id=casual.pk))) == 1

    def test_somebody_else_may_not_attach_to_it(self, client, other, casual):
        assert upload(Api(client, other), CASUAL, casual.pk).status_code == 403

    def test_finance_reads_it_but_does_not_add(self, client, tech, fin, casual):
        made = upload(Api(client, tech), CASUAL, casual.pk).json()
        as_fin = Api(client, fin)

        listed = results(as_fin.get("attachments", target_type=CASUAL, target_id=casual.pk))

        assert [row["id"] for row in listed] == [made["id"]]
        assert as_fin.get(f"attachments/{made['id']}/url").status_code == 200
        assert upload(as_fin, CASUAL, casual.pk).status_code == 403

    def test_another_member_sees_nothing_and_cannot_fetch_it(self, client, tech, other, casual):
        made = upload(Api(client, tech), CASUAL, casual.pk).json()
        as_other = Api(client, other)

        assert results(as_other.get("attachments", target_type=CASUAL, target_id=casual.pk)) == []
        assert as_other.get(f"attachments/{made['id']}").status_code == 404
        assert as_other.get(f"attachments/{made['id']}/url").status_code == 404

    def test_other_kinds_of_attachment_are_unaffected(self, client, tech, other, expense):
        """Only the casual's photo is restricted; an expense's receipt is open to read."""
        made = upload(Api(client, tech), EXPENSE, expense.pk).json()

        listed = results(
            Api(client, other).get("attachments", target_type=EXPENSE, target_id=expense.pk)
        )

        assert [row["id"] for row in listed] == [made["id"]]
