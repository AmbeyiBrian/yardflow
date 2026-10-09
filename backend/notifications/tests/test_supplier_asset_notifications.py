"""T17.9 — supplier and asset notifications (§4.20.9; R14, R15).

Who is told at each supplier step and what the message says, plus the
``asset.expiry_due`` event's recipients and wording.
"""

import pytest

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from network import suppliers
from notifications.events import render_body
from notifications.matrix import (
    DEFAULT_MATRIX,
    Channel,
    Event,
    Recipient,
    channels_for,
    default_channels_config,
    default_matrix_config,
)
from notifications.models import NotificationDelivery, NotificationEvent

FULL = {"kra_pin": "P051234567A", "bank_name": "KCB", "account_number": "123"}


@pytest.fixture(autouse=True)
def seeded_matrix(tenant):
    settings = tenant.settings
    settings.notification_matrix = default_matrix_config()
    settings.notification_channels = default_channels_config()
    settings.save()


@pytest.fixture
def registrar(tenant):
    return UserFactory(organization=tenant, full_name="Rita Registrar", email="rita@x.co.ke")


@pytest.fixture
def fiona(tenant):
    role = RoleFactory(name="Finance", codenames=[PERM.FINANCE_APPROVE])
    user = UserFactory(organization=tenant, full_name="Fiona Finance", email="fiona@x.co.ke")
    UserRoleFactory(user=user, role=role)
    return user


@pytest.fixture
def owner(tenant):
    role = RoleFactory(name="Owner", codenames=[PERM.USERS_MANAGE])
    user = UserFactory(organization=tenant, full_name="Olu Owner", email="olu@x.co.ke")
    UserRoleFactory(user=user, role=role)
    return user


def who(event_key, channel):
    return {
        d.recipient.pk
        for d in NotificationDelivery.objects.filter(event__event_key=event_key, channel=channel)
    }


def last_event(event_key):
    return NotificationEvent.objects.filter(event_key=event_key).latest("id")


@pytest.mark.django_db
class TestMatrix:
    def test_asset_expiry_goes_to_the_owner_in_app_and_email_only(self, tenant):
        spec = {s.key: s for s in DEFAULT_MATRIX}[Event.ASSET_EXPIRY_DUE]
        assert spec.recipients == (Recipient.OWNER,)
        assert set(spec.channels) == {Channel.IN_APP, Channel.EMAIL}
        assert Channel.SMS not in channels_for(tenant, Event.ASSET_EXPIRY_DUE)

    def test_a_tenant_seeded_before_the_event_still_gets_its_default(self, tenant):
        settings = tenant.settings
        settings.notification_matrix = {}
        settings.save()
        assert set(channels_for(tenant, Event.ASSET_EXPIRY_DUE)) == {
            Channel.IN_APP,
            Channel.EMAIL,
        }


@pytest.mark.django_db
class TestSupplierRecipientsAndWording:
    def test_awaiting_goes_to_finance_but_not_the_registrar(
        self, tenant, registrar, fiona, django_capture_on_commit_callbacks
    ):
        # The registrar also holds finance.approve here, and must not be asked.
        role = RoleFactory(name="Finance 2", codenames=[PERM.FINANCE_APPROVE])
        UserRoleFactory(user=registrar, role=role)
        with django_capture_on_commit_callbacks(execute=True):
            suppliers.add_supplier(actor=registrar, name="Kenya Cable Ltd")

        key = Event.FINANCE_AWAITING_APPROVAL
        assert who(key, Channel.IN_APP) == {fiona.pk}
        assert who(key, Channel.EMAIL) == {fiona.pk}
        body = render_body(last_event(key))
        assert body.startswith(
            "Supplier waiting for your approval: Kenya Cable Ltd added by Rita Registrar."
        )
        assert "Open Approvals" in body

    def test_approval_tells_the_registrar_in_app(
        self, tenant, registrar, fiona, django_capture_on_commit_callbacks
    ):
        supplier = suppliers.add_supplier(actor=registrar, name="Kenya Cable Ltd", **FULL)
        with django_capture_on_commit_callbacks(execute=True):
            suppliers.decide_supplier(supplier, actor=fiona, approved=True)

        key = Event.FINANCE_APPROVED
        assert who(key, Channel.IN_APP) == {registrar.pk}
        assert render_body(last_event(key)) == (
            "Supplier approved: Kenya Cable Ltd. They can now be paid."
        )

    def test_rejection_tells_the_registrar_with_the_reason(
        self, tenant, registrar, fiona, django_capture_on_commit_callbacks
    ):
        supplier = suppliers.add_supplier(actor=registrar, name="Kenya Cable Ltd")
        with django_capture_on_commit_callbacks(execute=True):
            suppliers.decide_supplier(supplier, actor=fiona, approved=False, reason="Wrong PIN")

        key = Event.FINANCE_REJECTED
        assert who(key, Channel.IN_APP) == {registrar.pk}
        assert who(key, Channel.EMAIL) == {registrar.pk}
        body = render_body(last_event(key))
        assert body.startswith("Supplier rejected: Kenya Cable Ltd.")
        assert "Reason: Wrong PIN" in body

    def test_a_sensitive_edit_tells_finance_and_names_the_fields(
        self, tenant, registrar, fiona, django_capture_on_commit_callbacks
    ):
        supplier = suppliers.add_supplier(actor=registrar, name="Kenya Cable Ltd", **FULL)
        suppliers.decide_supplier(supplier, actor=fiona, approved=True)
        with django_capture_on_commit_callbacks(execute=True):
            suppliers.update_supplier(
                supplier, actor=registrar, changes={"account_number": "999", "phone": "0700"}
            )

        key = Event.SUPPLIER_DETAILS_CHANGED
        assert who(key, Channel.IN_APP) == {fiona.pk}
        assert who(key, Channel.EMAIL) == {fiona.pk}
        body = render_body(last_event(key))
        assert body.startswith(
            "Supplier payment details changed: Kenya Cable Ltd by Rita Registrar."
        )
        assert "Changed: account number." in body
        assert "phone" not in body

    def test_a_non_sensitive_edit_says_nothing(self, tenant, registrar, fiona):
        supplier = suppliers.add_supplier(actor=registrar, name="Kenya Cable Ltd", **FULL)
        suppliers.decide_supplier(supplier, actor=fiona, approved=True)
        suppliers.update_supplier(supplier, actor=registrar, changes={"phone": "0700"})

        assert not NotificationEvent.objects.filter(
            event_key=Event.SUPPLIER_DETAILS_CHANGED
        ).exists()


@pytest.mark.django_db
class TestAssetExpiryWording:
    def make(self, tenant, owner, **payload):
        from assets.models import Asset, AssetType
        from notifications.events import emit

        asset, _ = Asset.objects.get_or_create(
            organization=tenant,
            type=AssetType.VEHICLE,
            name="Hilux",
            tag="KDA 123A",
        )
        base = {
            "asset": "Hilux",
            "tag": "KDA 123A",
            "document": "Insurance",
            "expires_on": "2026-11-01",
            "days_left": 12,
            "expired": False,
        }
        return emit(Event.ASSET_EXPIRY_DUE, asset, payload={**base, **payload})

    def test_upcoming(self, tenant, owner):
        assert render_body(self.make(tenant, owner)) == (
            "Insurance for Hilux (KDA 123A) expires on 2026-11-01 (in 12 days). "
            "Renew it and update the register."
        )

    def test_today_and_tomorrow(self, tenant, owner):
        assert "(today)" in render_body(self.make(tenant, owner, days_left=0))
        assert "(in 1 day)" in render_body(self.make(tenant, owner, days_left=1))

    def test_lapsed_reads_expired_on(self, tenant, owner):
        body = render_body(
            self.make(tenant, owner, expired=True, days_left=0, document="Inspection")
        )
        assert body.startswith("Inspection for Hilux (KDA 123A) expired on 2026-11-01.")

    def test_it_reaches_the_owner_not_finance(
        self, tenant, owner, fiona, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            self.make(tenant, owner)

        assert who(Event.ASSET_EXPIRY_DUE, Channel.IN_APP) == {owner.pk}
        assert who(Event.ASSET_EXPIRY_DUE, Channel.EMAIL) == {owner.pk}
