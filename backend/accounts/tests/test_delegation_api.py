"""Creating a delegation through the API refuses badly, not loudly (F5, §6.1).

Reported from use: delegating to yourself returned **500** with a page of
traceback. The check constraint did its job — `delegation_is_not_self` — but a
constraint is the last line, not the interface. An `IntegrityError` tells the
person who filled in the form nothing at all.

The constraints stay exactly as they are. This is the layer that says why, in
words, on the field that is wrong.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, User, UserRole
from core.provisioning import provision_tenant
from core.tenancy import tenant_context

pytestmark = pytest.mark.django_db


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


@pytest.fixture
def colleague(signed_in):
    _http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        person = User.objects.create_user(
            email="approver@silvertech.co.ke",
            password="a good long password",
            organization=organization,
            full_name="Alan Approver",
        )
        UserRole.objects.create(
            organization=organization,
            user=person,
            role=Role.objects.get(name="Approver"),
        )
    return person


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def create(http, token, **overrides):
    now = timezone.now()
    payload = {
        "starts_at": now.isoformat(),
        "ends_at": (now + timedelta(days=7)).isoformat(),
        "reason": "Annual leave",
    }
    payload.update(overrides)
    return http.post(
        reverse("v1:delegation-list"),
        payload,
        content_type="application/json",
        **auth(token),
    )


class TestWhatIsRefused:
    def test_delegating_to_yourself_is_a_message_not_a_crash(self, signed_in):
        """The report. A 500 here taught the user nothing and looked broken."""
        http, token, _organization, owner = signed_in

        response = create(http, token, from_user=owner.pk, to_user=owner.pk)

        assert response.status_code == 400, response.status_code
        # Attached to the field that is wrong, so the form can highlight it.
        assert "to_user" in response.json()["error"]["field_errors"]

    def test_an_end_before_the_start_is_a_message_too(self, signed_in, colleague):
        """The other check constraint, which would have failed the same way."""
        http, token, _organization, owner = signed_in
        now = timezone.now()

        response = create(
            http,
            token,
            from_user=owner.pk,
            to_user=colleague.pk,
            starts_at=now.isoformat(),
            ends_at=(now - timedelta(days=1)).isoformat(),
        )

        assert response.status_code == 400
        assert "ends_at" in response.json()["error"]["field_errors"]

    def test_somebody_from_another_tenant_is_refused(self, signed_in):
        """A delegation hands over authority, so both people must be inside this
        tenant. `User.objects` is not tenant-scoped — signing in has to find
        somebody before their organization is known — so this cannot be assumed.
        """
        http, token, _organization, owner = signed_in
        other = provision_tenant(
            name="Global Connect",
            slug="globalconnect",
            owner_email="owner@globalconnect.co.ke",
            send_invitation=False,
        )

        response = create(
            http,
            token,
            from_user=owner.pk,
            to_user=other["owner"].pk,
            codenames=["gate_out.approve"],
        )

        assert response.status_code == 400
        assert "to_user" in response.json()["error"]["field_errors"]


class TestWhatIsAllowed:
    def test_an_ordinary_delegation_is_created(self, signed_in, colleague):
        http, token, _organization, owner = signed_in

        response = create(
            http,
            token,
            from_user=owner.pk,
            to_user=colleague.pk,
            codenames=["gate_out.approve"],
        )

        assert response.status_code == 201, response.content
        assert response.json()["to_user_name"] == "Alan Approver"


class TestADelegationMustDelegateSomething:
    """Found from use: a delegation with no role and no permissions.

    It was created happily and the screen showed it as **open** — so somebody
    goes on leave believing their approvals are covered, and the delegate can do
    nothing. That is worse than a refusal: a refusal is visible on the day it
    happens, this is visible the day an approval is needed and nobody has it.
    """

    def test_one_with_neither_a_role_nor_permissions_is_refused(self, signed_in, colleague):
        http, token, _organization, owner = signed_in

        response = create(http, token, from_user=owner.pk, to_user=colleague.pk)

        assert response.status_code == 400, response.status_code
        assert "role" in response.json()["error"]["field_errors"]

    def test_a_role_is_enough(self, signed_in, colleague, organization_roles):
        http, token, _organization, owner = signed_in

        response = create(
            http,
            token,
            from_user=owner.pk,
            to_user=colleague.pk,
            role=organization_roles["Approver"],
        )

        assert response.status_code == 201, response.content
        assert response.json()["role_name"] == "Approver"

    def test_specific_permissions_are_enough(self, signed_in, colleague):
        """Lending only the authority actually needed, rather than a whole role."""
        http, token, _organization, owner = signed_in

        response = create(
            http,
            token,
            from_user=owner.pk,
            to_user=colleague.pk,
            codenames=["gate_out.approve"],
        )

        assert response.status_code == 201, response.content
        assert response.json()["codenames"] == ["gate_out.approve"]

    def test_what_is_delegated_is_visible(self, signed_in, colleague, organization_roles):
        """The screen could not say what a delegation conferred, which is how the
        empty one went unnoticed."""
        http, token, _organization, owner = signed_in
        create(
            http,
            token,
            from_user=owner.pk,
            to_user=colleague.pk,
            role=organization_roles["Approver"],
        )

        listing = http.get(reverse("v1:delegation-list"), **auth(token)).json()

        assert listing["results"][0]["role_name"] == "Approver"


@pytest.fixture
def organization_roles(signed_in):
    _http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        return {role.name: role.pk for role in Role.objects.all()}


class TestNobodyLendsWhatTheyDoNotHold:
    """A delegation hands over your own keys. It cannot cut new ones.

    Found by asking the obvious question of a real delegation on screen: an
    owner had lent the Storekeeper role, which is sound because an owner holds
    everything a storekeeper does. Nothing was checking that. A storekeeper
    lending the **Owner** role was accepted with a 201 — authority that had never
    existed anywhere, created by a form.
    """

    def test_a_storekeeper_cannot_lend_the_owner_role(
        self, signed_in, colleague, organization_roles
    ):
        http, token, organization, _owner = signed_in
        with tenant_context(organization):
            storekeeper = User.objects.create_user(
                email="store@silvertech.co.ke",
                password="a good long password",
                organization=organization,
                full_name="Storekeeper Sam",
            )
            UserRole.objects.create(
                organization=organization,
                user=storekeeper,
                role=Role.objects.get(name="Storekeeper"),
            )

        response = create(
            http,
            token,
            from_user=storekeeper.pk,
            to_user=colleague.pk,
            role=organization_roles["Owner"],
        )

        assert response.status_code == 400, response.status_code
        assert "cannot delegate" in str(response.json()["error"]["field_errors"])

    def test_an_owner_may_lend_a_lesser_role(self, signed_in, colleague, organization_roles):
        """The case on screen, and the reason it is correct: an owner holds
        everything a storekeeper does, so this lends a subset of their own
        authority rather than creating any."""
        http, token, _organization, owner = signed_in

        response = create(
            http,
            token,
            from_user=owner.pk,
            to_user=colleague.pk,
            role=organization_roles["Storekeeper"],
        )

        assert response.status_code == 201, response.content

    def test_a_permission_they_do_not_hold_is_refused(self, signed_in, colleague):
        http, token, organization, _owner = signed_in
        with tenant_context(organization):
            technician = User.objects.create_user(
                email="tech2@silvertech.co.ke",
                password="a good long password",
                organization=organization,
                full_name="Tom Technician",
            )
            UserRole.objects.create(
                organization=organization,
                user=technician,
                role=Role.objects.get(name="Technician"),
            )

        response = create(
            http,
            token,
            from_user=technician.pk,
            to_user=colleague.pk,
            codenames=["gate_out.approve"],
        )

        assert response.status_code == 400

    def test_borrowed_authority_cannot_be_lent_onward(
        self, signed_in, colleague, organization_roles
    ):
        """Sub-delegation. Measured against what the lender holds *directly*,
        because the further authority travels the less anybody can see who
        authorised the first step."""
        http, token, organization, owner = signed_in
        # The colleague is an Approver who has been lent the Owner role.
        create(
            http,
            token,
            from_user=owner.pk,
            to_user=colleague.pk,
            role=organization_roles["Owner"],
        )
        with tenant_context(organization):
            third = User.objects.create_user(
                email="third@silvertech.co.ke",
                password="a good long password",
                organization=organization,
                full_name="Third Person",
            )
            UserRole.objects.create(
                organization=organization,
                user=third,
                role=Role.objects.get(name="Technician"),
            )

        response = create(
            http,
            token,
            from_user=colleague.pk,
            to_user=third.pk,
            role=organization_roles["Owner"],
        )

        assert response.status_code == 400, "borrowed keys are not yours to lend"
