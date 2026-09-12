"""T8.8, T8.9 — WebAuthn enrolment and approval step-up (§5.3, §4.8; B5, F4, M3).

Two criteria:

* T8.8 — "a user enrols two devices and an admin can revoke one **without
  locking them out**"
* T8.9 — "**replaying an assertion against a different approval is rejected**",
  and the action records the credential used

The second is the security property, and it is tested at the level where it is
actually enforced: the challenge store. Faking a real authenticator's signature
is not possible in a unit test, so what these check is the binding — that a
challenge raised for approval A is not accepted for approval B, and that a
challenge is consumed by its first use. Those are the two ways a replay would
work, and both are ours to get right rather than the browser's.
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
    owner.full_name = "Sam Owner"
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


def enrol(organization, user, *, label: str, credential_id: str):
    """A credential as enrolment would have left it.

    Created directly rather than through the endpoint: attestation needs a real
    authenticator, and what these tests are about is what happens afterwards.
    """
    from accounts.models import WebAuthnCredential

    with tenant_context(organization):
        return WebAuthnCredential.objects.create(
            organization=organization,
            user=user,
            credential_id=credential_id,
            public_key="cHVibGljLWtleQ",
            device_label=label,
        )


class TestEnrolmentAndRevocation:
    """T8.8: two devices, and revoking one does not lock anybody out."""

    def test_registration_options_are_offered(self, signed_in):
        http, token, _organization, _owner = signed_in

        response = http.post(
            reverse("v1:auth:webauthn:webauthn-register-begin"),
            {"device_label": "Sam's phone"},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
        options = response.json()["options"]
        # A challenge and the relying party, which is what the browser needs.
        assert "challenge" in options
        assert "rp" in options

    def test_two_devices_can_be_enrolled_and_one_revoked(self, signed_in):
        http, token, organization, owner = signed_in
        first = enrol(organization, owner, label="Phone", credential_id="cred-one")
        second = enrol(organization, owner, label="Tablet", credential_id="cred-two")

        listing = http.get(reverse("v1:auth:webauthn:webauthn-credentials"), **auth(token))
        assert listing.status_code == 200, listing.content
        assert {row["device_label"] for row in listing.json()["results"]} == {
            "Phone",
            "Tablet",
        }

        revoked = http.post(
            reverse("v1:auth:webauthn:webauthn-credentials"),
            {"credential": first.pk},
            content_type="application/json",
            **auth(token),
        )
        assert revoked.status_code == 200, revoked.content
        assert revoked.json()["is_active"] is False

        # B5: the other still works, so a lost phone is not a lockout.
        with tenant_context(organization):
            second.refresh_from_db()
            assert second.is_active is True
            # And the revoked one is still on record, so an approval signed with
            # it stays explicable (M3).
            first.refresh_from_db()
            assert first.revoked_at is not None
            assert first.revoked_by_id == owner.pk

    def test_revoking_the_last_credential_is_allowed_and_is_not_a_lockout(self, signed_in):
        """A user with no authenticator falls back to a password (§5.3).

        That is a degradation, not a lockout — which is why revocation does not
        refuse to remove the last one. Refusing would mean a stolen phone stayed
        enrolled.
        """
        http, token, organization, owner = signed_in
        only = enrol(organization, owner, label="Only phone", credential_id="cred-only")

        response = http.post(
            reverse("v1:auth:webauthn:webauthn-credentials"),
            {"credential": only.pk},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200
        # And they can still approve: the approval endpoint's assertion is
        # optional, which the approval tests cover.

    def test_somebody_elses_credential_needs_users_manage(self, signed_in):
        from accounts.factories import UserFactory

        http, _token, organization, _owner = signed_in

        with tenant_context(organization):
            other = UserFactory(organization=organization, email="tech@silvertech.co.ke")
            other.set_password("a good long password")
            other.save()
        theirs = enrol(organization, other, label="Their phone", credential_id="cred-x")

        their_token = http.post(
            reverse("v1:auth:login"),
            {"identifier": "tech@silvertech.co.ke", "password": "a good long password"},
            content_type="application/json",
        ).json()["access"]

        # The technician cannot revoke the owner's, nor list it.
        response = http.get(
            reverse("v1:auth:webauthn:webauthn-credentials"),
            {"user": 1},
            **auth(their_token),
        )
        assert response.status_code in (403, 404)

        # But they can manage their own.
        own = http.post(
            reverse("v1:auth:webauthn:webauthn-credentials"),
            {"credential": theirs.pk},
            content_type="application/json",
            **auth(their_token),
        )
        assert own.status_code == 200


class TestTheAssertionIsBoundToItsApproval:
    """T8.9's criterion: a replay against another approval is rejected."""

    @pytest.fixture
    def two_pending_approvals(self, signed_in):
        """Two gate passes, each awaiting approval."""
        from django.db import transaction

        from accounts.factories import RoleFactory, UserRoleFactory
        from approvals.models import ApprovalRule
        from catalogue.factories import ItemTypeFactory
        from catalogue.models import Criticality
        from dispatch.models import GateOut, GateOutLine, GateOutPurpose
        from dispatch.services import submit_gate_out
        from locations.factories import YardFactory
        from locations.nodes import external_node, node_for_location
        from network.factories import SiteFactory
        from stock.models import MovementType
        from stock.services import MovementRequest, post_movement

        _http, _token, organization, owner = signed_in

        with tenant_context(organization):
            yard = YardFactory(name="Step-up yard")
            site = SiteFactory(internal_ref="SU-1", name="Step-up site")
            item = ItemTypeFactory(name="Step-up radio", uom="ea")
            item.category.criticality = Criticality.HIGH
            item.category.save(update_fields=["criticality"])

            role = RoleFactory(organization=organization, name="Step-up approver")
            UserRoleFactory(organization=organization, user=owner, role=role)
            ApprovalRule.objects.create(
                organization=organization,
                criticality=Criticality.HIGH,
                required_role=role,
                sequence=1,
            )

            with transaction.atomic():
                post_movement(
                    MovementRequest(
                        item_type=item,
                        quantity=Decimal("20"),
                        from_node=external_node(organization.pk),
                        to_node=node_for_location(yard),
                        movement_type=MovementType.RECEIPT,
                    )
                )

            # §5.3 refuses a self-approval, so somebody else raises these —
            # which is also how it happens: a storekeeper asks, an owner signs.
            from accounts.factories import UserFactory

            storekeeper = UserFactory(organization=organization, full_name="Sara Storekeeper")

            passes = []
            for index in range(2):
                gate_out = GateOut.objects.create(
                    organization=organization,
                    from_location=yard,
                    site=site,
                    custody_holder=owner,
                    requested_by=storekeeper,
                    purpose_type=GateOutPurpose.INSTALLATION,
                )
                GateOutLine.objects.create(
                    organization=organization,
                    gate_out=gate_out,
                    item_type=item,
                    tracking_mode="BULK",
                    requested_qty=Decimal(str(index + 1)),
                    uom="ea",
                )
                submit_gate_out(gate_out, submitted_by=storekeeper)
                passes.append(gate_out)
        return passes

    def test_a_challenge_is_raised_against_one_named_approval(
        self, signed_in, two_pending_approvals
    ):
        from approvals.engine import next_pending_request

        http, token, organization, _owner = signed_in
        first, _second = two_pending_approvals

        with tenant_context(organization):
            enrol(
                organization,
                _owner,
                label="Approving phone",
                credential_id="cred-approve",
            )
            pending = next_pending_request(first)

        response = http.post(
            reverse("v1:auth:webauthn:webauthn-assert-begin"),
            {"approval_request": pending.pk},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
        assert response.json()["approval_request"] == pending.pk
        assert "challenge" in response.json()["options"]

    def test_a_challenge_for_one_approval_is_not_valid_for_another(
        self, signed_in, two_pending_approvals
    ):
        """The replay T8.9 forbids, checked where it is actually stopped.

        A signature obtained for the one-unit pass must not authorise the
        two-unit one. The challenge store is keyed by approval id, so the second
        approval finds nothing and refuses.
        """
        from accounts.webauthn_service import ChallengeMismatch, _take_challenge
        from approvals.engine import next_pending_request

        _http, _token, organization, owner = signed_in
        first, second = two_pending_approvals

        with tenant_context(organization):
            from accounts.webauthn_service import _store_challenge, new_challenge_bytes

            first_pending = next_pending_request(first)
            second_pending = next_pending_request(second)

            _store_challenge(
                owner,
                new_challenge_bytes(),
                purpose="approve",
                approval_request=first_pending,
            )

            # The other approval has no challenge of its own, and the one raised
            # for the first is not offered to it.
            with pytest.raises(ChallengeMismatch):
                _take_challenge(owner, purpose="approve", approval_request=second_pending)

            # The rightful one still works, once.
            assert _take_challenge(owner, purpose="approve", approval_request=first_pending)

    def test_a_challenge_is_consumed_by_its_first_use(self, signed_in, two_pending_approvals):
        """Otherwise one fingerprint would authorise a document twice."""
        from accounts.webauthn_service import (
            ChallengeMismatch,
            _store_challenge,
            _take_challenge,
            new_challenge_bytes,
        )
        from approvals.engine import next_pending_request

        _http, _token, organization, owner = signed_in
        first, _second = two_pending_approvals

        with tenant_context(organization):
            pending = next_pending_request(first)
            _store_challenge(
                owner, new_challenge_bytes(), purpose="approve", approval_request=pending
            )

            _take_challenge(owner, purpose="approve", approval_request=pending)
            with pytest.raises(ChallengeMismatch):
                _take_challenge(owner, purpose="approve", approval_request=pending)

    def test_an_assertion_from_nowhere_is_refused_by_the_approval_endpoint(
        self, signed_in, two_pending_approvals
    ):
        """A fabricated assertion with no challenge behind it authorises nothing."""
        http, token, organization, owner = signed_in
        first, _second = two_pending_approvals

        with tenant_context(organization):
            enrol(organization, owner, label="Phone", credential_id="cred-approve")

        response = http.post(
            reverse("v1:gate-out-approve", args=[first.pk]),
            {"reason": "", "assertion": {"id": "cred-approve", "response": {}}},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code in (400, 409)
        assert response.json()["error"]["code"].startswith("WEBAUTHN")

        with tenant_context(organization):
            first.refresh_from_db()
            # And nothing was approved on the strength of it.
            assert first.status == "PENDING_APPROVAL"

    def test_approving_without_an_assertion_still_works(self, signed_in, two_pending_approvals):
        """§5.3: the fingerprint is a step-*up*.

        An owner on a laptop with no sensor still has to be able to approve, and
        what changes is what the action records — which an auditor can tell
        apart (M3).
        """
        http, token, organization, _owner = signed_in
        first, _second = two_pending_approvals

        response = http.post(
            reverse("v1:gate-out-approve", args=[first.pk]),
            {"reason": "Fine by me."},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
        assert response.json()["status"] == "APPROVED"

        with tenant_context(organization):
            from approvals.models import ApprovalAction

            action = ApprovalAction.objects.filter(
                approval_request__document_id=str(first.pk)
            ).latest("decided_at")
            assert action.auth_method == "PASSWORD"
            assert action.webauthn_credential_id is None
