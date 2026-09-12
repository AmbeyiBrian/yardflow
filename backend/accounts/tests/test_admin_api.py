"""The administration endpoints (§6, §4.2; B3, B4, C8).

T2.15's criterion lives here: "deactivating a user holding custody warns and
blocks until custody is cleared". The screen shows the warning; this is the
refusal behind it, because a warning a client could skip past would be decoration.
"""

from decimal import Decimal

import pytest
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


class TestUsers:
    def test_a_user_is_created_with_roles(self, signed_in):
        http, token, organization, _owner = signed_in

        with tenant_context(organization):
            from accounts.models import Role

            role = Role.objects.filter(name__icontains="store").first() or (
                Role.objects.create(organization=organization, name="Storekeeper")
            )

        response = http.post(
            reverse("v1:user-list"),
            {
                "email": "sara@silvertech.co.ke",
                "full_name": "Sara Storekeeper",
                "role_ids": [role.pk],
            },
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["roles"][0]["name"] == role.name
        # The resolved permissions come back, so the screen can show what the
        # assignment actually grants rather than what it looks like it grants.
        assert isinstance(body["permissions"], list)

    def test_is_active_cannot_be_patched(self, signed_in):
        """B3: deactivation carries guards, so it is an action not a field."""
        http, token, organization, _owner = signed_in

        with tenant_context(organization):
            from accounts.factories import UserFactory

            user = UserFactory(organization=organization, full_name="Patchable Pete")

        response = http.patch(
            reverse("v1:user-detail", args=[user.pk]),
            {"is_active": False},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.is_active is True

    def test_deactivating_a_holder_of_custody_is_refused(self, signed_in):
        """T2.15's stated criterion, and B3's edge case.

        The material would still be somewhere. Nobody would be accountable for
        it, which is the exact outcome the system exists to prevent.
        """
        http, token, organization, _owner = signed_in

        with tenant_context(organization):
            from django.db import transaction

            from accounts.factories import UserFactory
            from catalogue.factories import ItemTypeFactory
            from locations.nodes import external_node, node_for_user
            from stock.models import MovementType
            from stock.services import MovementRequest, post_movement

            holder = UserFactory(organization=organization, full_name="Holding Hilda")
            item = ItemTypeFactory(name="Admin torque wrench")
            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=item,
                        quantity=Decimal("1"),
                        from_node=external_node(organization.pk),
                        to_node=node_for_user(holder),
                        movement_type=MovementType.RECEIPT,
                    )
                )

        response = http.post(
            reverse("v1:user-deactivate", args=[holder.pk]),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400, response.content
        body = response.json()
        assert body["error"]["code"] == "HOLDER_STILL_HAS_MATERIAL"
        # The message has to say what they are holding, or the administrator
        # cannot act on it.
        assert body["error"]["details"]["holdings"][0]["item"] == "Admin torque wrench"

        holder.refresh_from_db()
        assert holder.is_active is True

    def test_deactivating_someone_holding_nothing_succeeds(self, signed_in):
        http, token, organization, _owner = signed_in

        with tenant_context(organization):
            from accounts.factories import UserFactory

            leaver = UserFactory(organization=organization, full_name="Leaving Larry")

        response = http.post(
            reverse("v1:user-deactivate", args=[leaver.pk]),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
        assert response.json()["is_active"] is False

    def test_deactivating_the_last_owner_is_refused(self, signed_in):
        """B4: leaving nobody able to administer the tenant is not allowed."""
        http, token, _organization, owner = signed_in

        response = http.post(
            reverse("v1:user-deactivate", args=[owner.pk]),
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "LAST_OWNER_PROTECTED"

    def test_someone_with_movements_cannot_be_deleted(self, signed_in):
        """B3: the ledger names who posted each movement, and it is append-only."""
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from django.db import transaction

            from catalogue.factories import ItemTypeFactory
            from locations.factories import YardFactory
            from locations.nodes import external_node
            from stock.models import MovementType
            from stock.services import MovementRequest, post_movement

            yard = YardFactory(name="Deletion yard")
            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=ItemTypeFactory(name="Posted item"),
                        quantity=Decimal("1"),
                        from_node=external_node(organization.pk),
                        to_node=yard.node,
                        movement_type=MovementType.RECEIPT,
                        posted_by=owner,
                    )
                )

        response = http.get(reverse("v1:user-detail", args=[owner.pk]), **auth(token))

        assert response.status_code == 200
        assert response.json()["can_be_deleted"] is False


class TestRoles:
    def test_a_role_is_created_with_its_permissions(self, signed_in):
        """B4: roles are data, permissions are code."""
        http, token, _organization, _owner = signed_in

        response = http.post(
            reverse("v1:role-list"),
            {
                "name": "Gate guard",
                "description": "Releases at the gate and nothing else (Q7).",
                "codenames": ["gate_out.release"],
            },
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 201, response.content
        assert response.json()["codenames"] == ["gate_out.release"]

    def test_an_invented_permission_is_rejected(self, signed_in):
        """A codename nothing checks would look like protection and be none."""
        http, token, _organization, _owner = signed_in

        response = http.post(
            reverse("v1:role-list"),
            {"name": "Wishful", "codenames": ["gate_out.approve_everything"]},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 400
        assert "codenames" in response.json()["error"]["field_errors"]

    def test_the_permission_groups_carry_their_rationale(self, signed_in):
        """§4.2: the role editor explains why each switch exists."""
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:permission-groups"), **auth(token))

        assert response.status_code == 200
        groups = {group["group"] for group in response.json()["groups"]}
        assert "Dispatch" in groups

        dispatch = next(
            group for group in response.json()["groups"] if group["group"] == "Dispatch"
        )
        release = next(
            permission
            for permission in dispatch["permissions"]
            if permission["codename"] == "gate_out.release"
        )
        assert "gate guard" in release["rationale"]


class TestSettings:
    def test_settings_are_readable_by_any_member(self, signed_in):
        """Half the screens change shape according to these switches."""
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:organization-settings"), **auth(token))

        assert response.status_code == 200
        assert response.json()["money_tracking_enabled"] is False
        assert response.json()["currency"] == "KES"

    def test_a_switch_can_be_changed(self, signed_in):
        """T2.14: toggling money tracking is what makes cost fields appear."""
        http, token, organization, _owner = signed_in

        response = http.patch(
            reverse("v1:organization-settings"),
            {"money_tracking_enabled": True},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
        assert response.json()["money_tracking_enabled"] is True

        # And it is audited: C8's switches change what the system enforces.
        from core.models import AuditLog

        with tenant_context(organization):
            assert AuditLog.objects.filter(
                target_label="Organization settings"
            ).exists()


class TestDelegation:
    def test_a_delegation_can_be_created_and_revoked(self, signed_in):
        """F5: revoked rather than deleted — someone may come back early."""
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from datetime import timedelta

            from django.utils import timezone

            from accounts.factories import UserFactory
            from accounts.models import Role

            delegate = UserFactory(organization=organization, full_name="Deputy Dan")
            role = Role.objects.filter(organization=organization).first()
            now = timezone.now()

        created = http.post(
            reverse("v1:delegation-list"),
            {
                "from_user": owner.pk,
                "to_user": delegate.pk,
                "role": role.pk,
                "starts_at": now.isoformat(),
                "ends_at": (now + timedelta(days=5)).isoformat(),
                "reason": "Away at a site build.",
            },
            content_type="application/json",
            **auth(token),
        )

        assert created.status_code == 201, created.content
        assert created.json()["is_currently_active"] is True

        revoked = http.post(
            reverse("v1:delegation-revoke", args=[created.json()["id"]]),
            content_type="application/json",
            **auth(token),
        )

        assert revoked.status_code == 200
        assert revoked.json()["is_revoked"] is True
        assert revoked.json()["is_currently_active"] is False


class TestSettingsAreActuallyGuarded:
    """Declaring a permission has to be the same thing as enforcing one.

    `OrganizationSettingsView` declared `{"patch": PERM.SETTINGS_MANAGE}` and
    enforced nothing for months: `HasPermission` resolved the codename from
    `view.action`, which DRF sets on ViewSets and leaves as `None` on a plain
    `APIView`. The map was skipped, no codename was found, and the check returned
    True for anyone signed in — so a technician could turn on self-approval,
    stretch the gate-pass expiry window, or shorten retention.

    Found while making the notification matrix editable, by writing the "and a
    storekeeper may not" half of a permission test and watching it pass with 200.
    """

    def test_a_member_without_the_permission_cannot_change_settings(self, signed_in):
        http, _owner_token, organization, _owner = signed_in

        with tenant_context(organization):
            from accounts.models import Role, User, UserRole

            technician = User.objects.create_user(
                email="tech@silvertech.co.ke",
                password="a good long password",
                organization=organization,
            )
            UserRole.objects.create(
                organization=organization,
                user=technician,
                role=Role.objects.get(name="Technician"),
            )

        token = http.post(
            reverse("v1:auth:login"),
            {"identifier": "tech@silvertech.co.ke", "password": "a good long password"},
            content_type="application/json",
        ).json()["access"]

        response = http.patch(
            reverse("v1:organization-settings"),
            {"allow_self_approval": True},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 403, (
            "self-approval is the control this system exists to provide"
        )
        organization.settings.refresh_from_db()
        assert organization.settings.allow_self_approval is False

    def test_reading_settings_stays_open_to_every_member(self, signed_in):
        """Half the screens change shape according to these switches, so a member
        who cannot change them must still be able to read them."""
        http, _owner_token, organization, _owner = signed_in

        with tenant_context(organization):
            from accounts.models import Role, User, UserRole

            technician = User.objects.create_user(
                email="reader@silvertech.co.ke",
                password="a good long password",
                organization=organization,
            )
            UserRole.objects.create(
                organization=organization,
                user=technician,
                role=Role.objects.get(name="Technician"),
            )

        token = http.post(
            reverse("v1:auth:login"),
            {"identifier": "reader@silvertech.co.ke", "password": "a good long password"},
            content_type="application/json",
        ).json()["access"]

        response = http.get(reverse("v1:organization-settings"), **auth(token))

        assert response.status_code == 200


class TestResendingAnInvitation:
    """A tenant administrator can send it again (B1).

    An account is created with no password and the person sets their own from a
    link. Links get lost — a phone is wiped, an email goes to spam — and without
    this the only route was to ask the platform owner, which is not tenant
    administration of your own staff.
    """

    def _add_someone(self, http, token, organization, email="new@silvertech.co.ke"):
        with tenant_context(organization):
            from accounts.models import Role

            role_id = Role.objects.get(name="Storekeeper").pk

        response = http.post(
            reverse("v1:user-list"),
            {"email": email, "full_name": "New Person", "role_ids": [role_id]},
            content_type="application/json",
            **auth(token),
        )
        assert response.status_code == 201, response.content
        return response.json()["id"]

    def test_a_new_account_is_shown_as_not_yet_signed_in(self, signed_in):
        """So the administrator knows the invitation is still outstanding,
        rather than wondering why the person cannot get in."""
        http, token, organization, _owner = signed_in
        user_id = self._add_someone(http, token, organization)

        response = http.get(reverse("v1:user-detail", args=[user_id]), **auth(token))

        assert response.json()["has_signed_in_yet"] is False

    def test_the_invitation_can_be_sent_again(self, signed_in, mailoutbox):
        http, token, organization, _owner = signed_in
        user_id = self._add_someone(http, token, organization)
        before = len(mailoutbox)

        response = http.post(
            reverse("v1:user-resend-invitation", args=[user_id]), **auth(token)
        )

        assert response.status_code == 200, response.content
        assert len(mailoutbox) == before + 1
        # Addressed to the tenant's own subdomain, whoever pressed the button.
        assert "silvertech.localhost" in mailoutbox[-1].body

    def test_no_password_is_created_by_sending_it(self, signed_in):
        """The whole point of the invitation flow: nobody but the person ever
        knows their password."""
        http, token, organization, _owner = signed_in
        user_id = self._add_someone(http, token, organization)

        http.post(reverse("v1:user-resend-invitation", args=[user_id]), **auth(token))

        with tenant_context(organization):
            from accounts.models import User

            assert User.objects.get(pk=user_id).has_usable_password() is False

    def test_a_deactivated_account_is_refused(self, signed_in):
        """Sending an invitation to somebody who cannot sign in would be a link
        that fails at the end, with no explanation."""
        http, token, organization, _owner = signed_in
        user_id = self._add_someone(http, token, organization)
        http.post(reverse("v1:user-deactivate", args=[user_id]), **auth(token))

        response = http.post(
            reverse("v1:user-resend-invitation", args=[user_id]), **auth(token)
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "USER_INACTIVE"


class TestAddingSomebodyWhoIsAlreadyThere:
    """Reported from use: adding a colleague with a phone number that was
    already on file returned **500** — "The server had a problem with that" —
    when the truth was that the number belonged to somebody in the row behind
    the dialog. The unique constraint did its job; nothing asked first.

    Naming who holds it is the whole answer, because the usual cause is the same
    person being added twice.
    """

    def _add(self, http, token, organization, **fields):
        with tenant_context(organization):
            from accounts.models import Role

            role_id = Role.objects.get(name="Storekeeper").pk

        payload = {"full_name": "Someone", "role_ids": [role_id]}
        payload.update(fields)
        return http.post(
            reverse("v1:user-list"),
            payload,
            content_type="application/json",
            **auth(token),
        )

    def test_a_phone_already_in_use_names_who_has_it(self, signed_in):
        http, token, organization, _owner = signed_in
        self._add(
            http, token, organization, full_name="Brian Technician", phone="+254797259698"
        )

        response = self._add(
            http, token, organization, full_name="Brian Storekeeper", phone="+254797259698"
        )

        assert response.status_code == 400, response.status_code
        assert "Brian Technician" in str(response.json()["error"]["field_errors"]["phone"])

    def test_the_same_number_written_differently_is_still_the_same_number(
        self, signed_in
    ):
        """Stored normalised, so `0797…` and `+254797…` are one number. Comparing
        the raw text would let the duplicate through to the database, which is
        where this started."""
        http, token, organization, _owner = signed_in
        self._add(http, token, organization, phone="+254797259698")

        response = self._add(http, token, organization, phone="0797259698")

        assert response.status_code == 400

    def test_an_email_already_in_use_is_refused_too(self, signed_in):
        http, token, organization, _owner = signed_in
        self._add(http, token, organization, email="taken@silvertech.co.ke")

        response = self._add(http, token, organization, email="taken@silvertech.co.ke")

        assert response.status_code == 400
        assert "email" in response.json()["error"]["field_errors"]

    def test_case_does_not_let_a_duplicate_through(self, signed_in):
        http, token, organization, _owner = signed_in
        self._add(http, token, organization, email="taken@silvertech.co.ke")

        response = self._add(http, token, organization, email="TAKEN@silvertech.co.ke")

        assert response.status_code == 400

    def test_another_tenant_may_use_the_same_number(self, signed_in):
        """Uniqueness is per organization: two companies can each employ someone
        with that phone, and often the same person contracts for both."""
        http, token, organization, _owner = signed_in
        self._add(http, token, organization, phone="+254797259698")

        other = provision_tenant(
            name="Global Connect",
            slug="globalconnect",
            owner_email="owner@globalconnect.co.ke",
            send_invitation=False,
        )
        with tenant_context(other["organization"]):
            from accounts.models import User

            assert User.objects.create_user(
                phone="+254797259698",
                organization=other["organization"],
                full_name="Same Number Elsewhere",
            )

    def test_editing_somebody_does_not_clash_with_themselves(self, signed_in):
        """Their own number is not a duplicate of itself."""
        http, token, organization, _owner = signed_in
        created = self._add(http, token, organization, phone="+254797259698").json()

        response = http.patch(
            reverse("v1:user-detail", args=[created["id"]]),
            {"full_name": "Renamed", "phone": "+254797259698"},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
