"""T4.1–T4.14 — gate-out, approval and release (§4.7, §5; F1–F8, G1–G3).

Milestone M4: "approved, auditable gate-outs. **This is the product.**"

The assertions that matter most are the refusals — an unapproved pass cannot be
released, a requester cannot approve their own request, an amended pass loses its
approval. If any of those can be walked around, nothing else here is worth
anything.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from freezegun import freeze_time

from accounts.factories import DelegationFactory, RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals.engine import (
    NotAnApprover,
    SelfApprovalNotAllowed,
    collect_facts,
    highest_of,
    predicate_matches,
    required_levels,
)
from approvals.models import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalRequestStatus,
    ApprovalRule,
)
from catalogue.factories import ItemCategoryFactory, ItemTypeFactory
from catalogue.models import Criticality, TrackingMode
from core.exceptions import InvalidTransition
from dispatch.models import (
    ALLOWED_TRANSITIONS,
    GateOut,
    GateOutLine,
    GateOutPurpose,
    GateOutStatus,
    ReleaseVariance,
)
from dispatch.services import (
    GateOutNotReady,
    ReleaseNotPermitted,
    acknowledge_variance,
    amend_gate_out,
    approve_gate_out,
    cancel_gate_out,
    close_gate_out,
    escalate_overdue_approvals,
    expire_stale_passes,
    reject_gate_out,
    release_gate_out,
    submit_gate_out,
)
from locations.factories import YardFactory
from locations.nodes import external_node, node_for_user
from network.factories import ClientFactory, SiteFactory
from stock.models import Condition, MovementType, OwnerType
from stock.services import MovementRequest, balance_at, post_movement

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def storekeeper(tenant):
    return UserFactory(organization=tenant, full_name="Sara Storekeeper")


@pytest.fixture
def technician(tenant):
    return UserFactory(organization=tenant, full_name="Tom Technician")


@pytest.fixture
def owner_role(tenant):
    return RoleFactory(name="Owner", codenames=[PERM.GATE_OUT_APPROVE])


@pytest.fixture
def owner(tenant, owner_role):
    person = UserFactory(organization=tenant, full_name="Sam Owner")
    UserRoleFactory(user=person, role=owner_role)
    return person


def stock_in(tenant, node, item, quantity, *, owner_client=None, condition=Condition.NEW):
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal(str(quantity)),
                from_node=external_node(tenant.pk, client=owner_client),
                to_node=node,
                movement_type=MovementType.RECEIPT,
                owner_type=OwnerType.CLIENT if owner_client else OwnerType.OWN,
                owner_client=owner_client,
                condition=condition,
            )
        )


def make_gate_out(tenant, yard, requester, holder, *, site=None, **kwargs):
    kwargs.setdefault("purpose_type", GateOutPurpose.INSTALLATION)
    if site is None and not any(kwargs.get(key) for key in ("work_order", "client", "to_location")):
        site = SiteFactory()
    return GateOut.objects.create(
        organization=tenant,
        from_location=yard,
        site=site,
        custody_holder=holder,
        requested_by=requester,
        **kwargs,
    )


def add_line(gate_out, item, quantity, *, returnable=False, owner_client=None, **kwargs):
    return GateOutLine.objects.create(
        organization=gate_out.organization,
        gate_out=gate_out,
        item_type=item,
        tracking_mode=kwargs.pop("tracking_mode", TrackingMode.BULK),
        requested_qty=Decimal(str(quantity)),
        uom=item.uom,
        is_returnable=returnable,
        owner_type=OwnerType.CLIENT if owner_client else OwnerType.OWN,
        owner_client=owner_client,
        **kwargs,
    )


def rule_for(criticality, role, sequence=1):
    return ApprovalRule.objects.create(
        criticality=criticality, required_role=role, sequence=sequence
    )


# --------------------------------------------------------------------------
# T4.1 — the document
# --------------------------------------------------------------------------


class TestDestinationArity:
    """F1: exactly one destination."""

    def test_a_gate_pass_needs_a_destination(self, tenant, yard, storekeeper, technician):
        with pytest.raises(ValidationError, match="exactly one destination"):
            GateOut.objects.create(
                organization=tenant,
                from_location=yard,
                custody_holder=technician,
                requested_by=storekeeper,
                purpose_type=GateOutPurpose.INSTALLATION,
            )

    def test_two_destinations_are_refused(self, tenant, yard, storekeeper, technician):
        """A pass with two destinations cannot be reconciled against either (H4)."""
        with pytest.raises(ValidationError, match="exactly one destination"):
            GateOut.objects.create(
                organization=tenant,
                from_location=yard,
                site=SiteFactory(),
                client=ClientFactory(),
                custody_holder=technician,
                requested_by=storekeeper,
                purpose_type=GateOutPurpose.INSTALLATION,
            )

    def test_the_database_enforces_the_arity_too(self, tenant, yard, storekeeper, technician):
        """Defence in depth: the constraint holds against a raw insert."""
        from django.db import connection

        site = SiteFactory()
        client = ClientFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO dispatch_gateout "
                    "(organization_id, number, status, purpose_type, site_id, client_id, "
                    " from_location_id, custody_holder_id, requested_by_id, version, "
                    " vehicle_reg, driver_name, reject_reason, cancel_reason, close_reason, "
                    " notes, created_at, updated_at) "
                    "VALUES (%s, '', 'DRAFT', 'INSTALLATION', %s, %s, %s, %s, %s, 1, "
                    " '', '', '', '', '', '', now(), now())",
                    [
                        str(tenant.pk),
                        site.pk,
                        client.pk,
                        yard.pk,
                        technician.pk,
                        storekeeper.pk,
                    ],
                )

    def test_a_return_to_client_must_name_the_client(self, tenant, yard, storekeeper, technician):
        with pytest.raises(ValidationError, match="must name the client"):
            GateOut.objects.create(
                organization=tenant,
                from_location=yard,
                site=SiteFactory(),
                custody_holder=technician,
                requested_by=storekeeper,
                purpose_type=GateOutPurpose.RETURN_TO_CLIENT,
            )


# --------------------------------------------------------------------------
# T4.2 — the status machine
# --------------------------------------------------------------------------


class TestStatusMachine:
    """§4.7: every transition in one place, and no view sets status directly."""

    def test_a_draft_can_be_submitted(self, tenant, yard, storekeeper, technician):
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)

        gate_out.transition(GateOutStatus.PENDING_APPROVAL)

        assert gate_out.status == GateOutStatus.PENDING_APPROVAL

    def test_a_draft_cannot_jump_straight_to_released(self, tenant, yard, storekeeper, technician):
        """The transition that would defeat the entire product."""
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)

        with pytest.raises(InvalidTransition):
            gate_out.transition(GateOutStatus.RELEASED)

    def test_a_rejected_request_cannot_become_approved(self, tenant, yard, storekeeper, technician):
        """It must go back through routing (F6), not simply flip status."""
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        gate_out.transition(GateOutStatus.PENDING_APPROVAL)
        gate_out.transition(GateOutStatus.REJECTED, reason="Wrong site")

        with pytest.raises(InvalidTransition):
            gate_out.transition(GateOutStatus.APPROVED)

    def test_terminal_statuses_go_nowhere(self, tenant, yard, storekeeper, technician):
        assert ALLOWED_TRANSITIONS[GateOutStatus.CLOSED] == ()
        assert ALLOWED_TRANSITIONS[GateOutStatus.CANCELLED] == ()

    def test_rejection_requires_a_reason(self, tenant, yard, storekeeper, technician):
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        gate_out.transition(GateOutStatus.PENDING_APPROVAL)

        with pytest.raises(InvalidTransition, match="requires a reason"):
            gate_out.transition(GateOutStatus.REJECTED)

    def test_cancellation_requires_a_reason(self, tenant, yard, storekeeper, technician):
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)

        with pytest.raises(InvalidTransition, match="requires a reason"):
            gate_out.transition(GateOutStatus.CANCELLED)

    def test_every_documented_transition_is_reachable(self):
        """§4.7's diagram and this map must not drift apart."""
        for status, targets in ALLOWED_TRANSITIONS.items():
            assert status in GateOutStatus.values
            for target in targets:
                assert target in GateOutStatus.values


# --------------------------------------------------------------------------
# T4.3, T4.4 — routing
# --------------------------------------------------------------------------


class TestRouting:
    """F3: criticality -> required role, highest wins."""

    def test_no_matching_rule_means_no_approval_needed(self, tenant, yard, storekeeper, technician):
        """§5.2: zero matched rules means auto-approval."""
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.NONE))
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 5)

        assert required_levels(gate_out) == []

    def test_a_high_criticality_line_routes_to_the_owner(
        self, tenant, yard, storekeeper, technician, owner_role
    ):
        """T4.6's criterion: a HIGH line routes to the owner."""
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 1)

        levels = required_levels(gate_out)

        assert [level.role for level in levels] == [owner_role]

    def test_a_mixed_document_takes_the_highest_level(
        self, tenant, yard, storekeeper, technician, owner_role
    ):
        """F3: "a gate-out containing lines from several categories takes the
        **highest** applicable approval level"."""
        supervisor_role = RoleFactory(name="Supervisor", codenames=[PERM.GATE_OUT_APPROVE])
        rule_for(Criticality.MEDIUM, supervisor_role, sequence=2)
        rule_for(Criticality.HIGH, owner_role, sequence=3)

        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(
            gate_out,
            ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.MEDIUM)),
            5,
        )
        add_line(
            gate_out,
            ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH)),
            1,
            line_number=2,
        )

        levels = required_levels(gate_out)

        assert [level.role for level in levels] == [owner_role]

    def test_criticality_ordering(self):
        assert highest_of({Criticality.LOW, Criticality.HIGH}) == Criticality.HIGH
        assert highest_of({Criticality.NONE, Criticality.MEDIUM}) == Criticality.MEDIUM
        assert highest_of(set()) == Criticality.NONE

    def test_inherited_criticality_is_used(self, tenant, yard, storekeeper, technician, owner_role):
        """C1: a subcategory inherits its parent's criticality.

        Without this, adding a subcategory would quietly drop the approval
        requirement on everything in it.
        """
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        parent = ItemCategoryFactory(name="Active equipment", criticality=Criticality.HIGH)
        child = ItemCategoryFactory(name="Radios", parent=parent)

        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, ItemTypeFactory(category=child), 1)

        assert [level.role for level in required_levels(gate_out)] == [owner_role]


class TestFactCollection:
    """F3: the fact set is complete even though only criticality is consulted."""

    def test_the_facts_include_dimensions_v1_does_not_use(
        self, tenant, yard, storekeeper, technician
    ):
        """This is what keeps future dimensions migration-free."""
        safaricom = ClientFactory(name="Safaricom")
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(
            gate_out,
            ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH)),
            3,
            owner_client=safaricom,
        )

        facts = collect_facts(gate_out)

        assert facts.highest_criticality == Criticality.HIGH
        assert facts.involves_client_owned is True
        assert facts.client_ids == {safaricom.pk}
        assert facts.total_quantity == Decimal("3")
        assert facts.destination_type == "SITE"
        assert facts.purpose_type == GateOutPurpose.INSTALLATION

    def test_monetary_value_is_none_when_money_tracking_is_off(
        self, tenant, yard, storekeeper, technician
    ):
        """D16: money tracking is off by default, so there is no value to route on."""
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, ItemTypeFactory(unit_cost=Decimal("100")), 2)

        assert collect_facts(gate_out).monetary_value is None

    def test_monetary_value_is_computed_when_money_tracking_is_on(
        self, tenant, yard, storekeeper, technician
    ):
        settings = tenant.settings
        settings.money_tracking_enabled = True
        settings.save()

        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, ItemTypeFactory(unit_cost=Decimal("100")), 2)

        assert collect_facts(gate_out).monetary_value == Decimal("200")


class TestPredicateEvaluator:
    """F3: conditions are empty in v1, but the evaluator is ready."""

    def test_empty_conditions_match_everything(self, tenant, yard, storekeeper, technician):
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, ItemTypeFactory(), 1)

        assert predicate_matches({}, collect_facts(gate_out)) is True

    def test_a_client_owned_condition_can_be_switched_on_later(
        self, tenant, yard, storekeeper, technician
    ):
        """The dimension F3 names first, working without a migration."""
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, ItemTypeFactory(), 1, owner_client=ClientFactory())

        facts = collect_facts(gate_out)

        assert predicate_matches({"involves_client_owned": True}, facts) is True
        assert predicate_matches({"involves_client_owned": False}, facts) is False

    def test_a_quantity_threshold_can_be_switched_on_later(
        self, tenant, yard, storekeeper, technician
    ):
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, ItemTypeFactory(), 10)
        facts = collect_facts(gate_out)

        assert predicate_matches({"min_total_quantity": 5}, facts) is True
        assert predicate_matches({"min_total_quantity": 50}, facts) is False

    def test_an_unknown_condition_fails_closed(self, tenant, yard, storekeeper, technician):
        """A condition nothing understands must not reduce the approval needed.

        Failing open would let a typo in a rule silently remove a control.
        """
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, ItemTypeFactory(), 1)

        assert predicate_matches({"nonsense_dimension": True}, collect_facts(gate_out)) is False


# --------------------------------------------------------------------------
# T4.6, T4.7, T4.8 — submit, approve, reject
# --------------------------------------------------------------------------


class TestSubmit:
    def test_submitting_allocates_a_number_and_routes(
        self, tenant, yard, storekeeper, technician, owner_role
    ):
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)

        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)

        submit_gate_out(gate_out, submitted_by=storekeeper)

        assert gate_out.number.startswith("GP-")
        assert gate_out.status == GateOutStatus.PENDING_APPROVAL
        assert ApprovalRequest.objects.filter(document_id=str(gate_out.pk)).count() == 1

    def test_a_consumables_only_request_auto_approves(self, tenant, yard, storekeeper, technician):
        """T4.6's criterion. PPE must not need the owner's signature, or people
        learn to route around the approval flow entirely."""
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.NONE))
        stock_in(tenant, yard.node, item, 10)

        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)

        submit_gate_out(gate_out, submitted_by=storekeeper)

        assert gate_out.status == GateOutStatus.APPROVED
        assert gate_out.expires_at is not None

    def test_an_auto_approval_still_writes_an_action(self, tenant, yard, storekeeper, technician):
        """§5.2: "so the audit trail never has a gap".

        "Why did this leave without approval?" must have an answer.
        """
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.NONE))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)

        submit_gate_out(gate_out, submitted_by=storekeeper)

        action = ApprovalAction.objects.get(decision=ApprovalDecision.AUTO)
        assert action.actor is None
        assert "no approval rule" in action.reason.lower()

    def test_submitting_more_than_is_in_stock_is_refused(
        self, tenant, yard, storekeeper, technician
    ):
        """Sending an approver an unreleasable request trains them to approve
        without looking."""
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 1)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 5)

        with pytest.raises(GateOutNotReady) as caught:
            submit_gate_out(gate_out, submitted_by=storekeeper)

        assert "lines.0.requested_qty" in caught.value.field_errors

    def test_amending_with_no_rules_does_not_strand_the_pass(
        self, tenant, yard, storekeeper, technician
    ):
        """From a pass stuck in a live yard.

        GP-000001 auto-approved on submission, because that organization has no
        approval rule at all, and was amended half a minute later. Amending
        voids the approval and re-runs routing, which is right — otherwise a
        small request could be approved and then enlarged. But re-routing
        produced nothing to approve, so it sat in "pending approval" with no
        approver, no queue entry and no screen that could move it. The requester
        had a notification saying approved and a list saying pending.
        """
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        assert gate_out.status == GateOutStatus.APPROVED, "no rule, so it auto-approves"

        amend_gate_out(gate_out, amended_by=storekeeper)

        assert gate_out.status == GateOutStatus.APPROVED, (
            "nobody can approve it, so leaving it pending strands it for ever"
        )
        assert gate_out.approved_at is not None
        assert gate_out.expires_at is not None, "and it expires like any other pass"

    def test_amending_with_a_rule_still_needs_approving_again(
        self, tenant, yard, storekeeper, technician, owner_role
    ):
        """The half that must not change: where there *is* somebody to ask,
        amending sends it back to them."""
        item = ItemTypeFactory(
            category=ItemCategoryFactory(criticality=Criticality.HIGH),
        )
        rule_for(Criticality.HIGH, owner_role)
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)
        submit_gate_out(gate_out, submitted_by=storekeeper)

        amend_gate_out(gate_out, amended_by=storekeeper)

        assert gate_out.status == GateOutStatus.PENDING_APPROVAL
        assert ApprovalRequest.objects.filter(
            document_type="dispatch.GateOut",
            document_id=str(gate_out.pk),
            status=ApprovalRequestStatus.PENDING,
        ).exists(), "there is somebody to ask, so it is asked"

    def test_the_refusal_says_whose_stock_is_actually_there(
        self, tenant, yard, storekeeper, technician
    ):
        """From a real confusion in the yard.

        A technician asked for three vests. The stock screen showed five at the
        yard and the request was refused for want of stock. Both were right: the
        five belonged to a client and the line asked for the company’s own,
        which is a different pile of vests. "Some lines ask for more than is in
        stock" is true and useless — it sends somebody to argue with the stock
        screen, which is not wrong either.
        """
        client = ClientFactory(name="Safaricom")
        item = ItemTypeFactory(name="High-visibility vest", uom="ea")
        stock_in(tenant, yard.node, item, 5, owner_client=client)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 3)  # own stock, of which there is none

        with pytest.raises(GateOutNotReady) as caught:
            submit_gate_out(gate_out, submitted_by=storekeeper)

        message = str(caught.value)
        assert "your own stock" in message, "what was asked for"
        assert "Safaricom" in message, "and what is actually on the shelf"
        assert "5 ea" in message

    def test_the_shortfall_is_in_the_banner_not_only_the_field(
        self, tenant, yard, storekeeper, technician
    ):
        """The screen shows the envelope message. Burying the useful sentence
        in the field errors left "some lines ask for more than is in stock" as
        the only thing on screen."""
        item = ItemTypeFactory(name="Cable clamp", uom="ea")
        stock_in(tenant, yard.node, item, 1)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 5)

        with pytest.raises(GateOutNotReady) as caught:
            submit_gate_out(gate_out, submitted_by=storekeeper)

        assert "Cable clamp" in str(caught.value)
        assert "asks for 5 ea" in str(caught.value)

    def test_a_matching_owner_passes(self, tenant, yard, storekeeper, technician):
        """The other half: once the line names the client, the same five vests
        are exactly what it asks for."""
        client = ClientFactory(name="Safaricom")
        item = ItemTypeFactory(name="High-visibility vest", uom="ea")
        stock_in(tenant, yard.node, item, 5, owner_client=client)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 3, owner_client=client)

        submit_gate_out(gate_out, submitted_by=storekeeper)

        assert gate_out.status != GateOutStatus.DRAFT

    def test_an_empty_request_cannot_be_submitted(self, tenant, yard, storekeeper, technician):
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)

        with pytest.raises(GateOutNotReady, match="at least one line"):
            submit_gate_out(gate_out, submitted_by=storekeeper)


class TestApproveAndReject:
    @pytest.fixture
    def pending(self, tenant, yard, storekeeper, technician, owner_role):
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        return gate_out, item

    def test_approving_the_final_level_approves_the_pass(self, pending, owner, tenant):
        """T4.8: "approving the final level sets APPROVED and expires_at"."""
        gate_out, _item = pending

        approve_gate_out(gate_out, actor=owner)

        assert gate_out.status == GateOutStatus.APPROVED
        assert gate_out.approved_at is not None
        assert gate_out.expires_at is not None

    def test_the_expiry_comes_from_the_tenant_setting(self, pending, owner, tenant):
        """Q3: configurable, default 24 hours."""
        settings = tenant.settings
        settings.gate_pass_expiry_hours = 4
        settings.save()
        gate_out, _item = pending

        with freeze_time("2026-03-01 08:00:00"):
            approve_gate_out(gate_out, actor=owner)

        assert gate_out.expires_at.hour == 12

    def test_the_approval_records_who_and_how(self, pending, owner):
        """§4.8: the non-repudiation evidence an ISO auditor asks for (F4, M3)."""
        from core.models import AuthMethod

        gate_out, _item = pending

        approve_gate_out(gate_out, actor=owner, auth_method=AuthMethod.WEBAUTHN)

        action = ApprovalAction.objects.get(decision=ApprovalDecision.APPROVED)
        assert action.actor == owner
        assert action.auth_method == AuthMethod.WEBAUTHN
        assert action.on_behalf_of is None

    def test_rejecting_requires_a_reason(self, pending, owner):
        gate_out, _item = pending

        with pytest.raises(GateOutNotReady, match="requires a reason"):
            reject_gate_out(gate_out, actor=owner, reason="")

    def test_rejecting_records_the_reason_and_stops_the_pass(self, pending, owner):
        gate_out, _item = pending

        reject_gate_out(gate_out, actor=owner, reason="Wrong site on the request")

        assert gate_out.status == GateOutStatus.REJECTED
        assert gate_out.reject_reason == "Wrong site on the request"
        assert ApprovalAction.objects.filter(decision=ApprovalDecision.REJECTED).exists()

    def test_someone_without_the_role_cannot_approve(self, pending, tenant):
        gate_out, _item = pending
        bystander = UserFactory(organization=tenant, full_name="Ann Admin")

        with pytest.raises(NotAnApprover):
            approve_gate_out(gate_out, actor=bystander)

        assert gate_out.status == GateOutStatus.PENDING_APPROVAL


class TestSelfApprovalGuard:
    """F3's edge case, default off: "a requester who also holds approval rights
    must not approve their own request"."""

    @pytest.fixture
    def own_request(self, tenant, yard, technician, owner_role, owner):
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        # The owner raises it themselves.
        gate_out = make_gate_out(tenant, yard, owner, technician)
        add_line(gate_out, item, 1)
        submit_gate_out(gate_out, submitted_by=owner)
        return gate_out

    def test_a_requester_cannot_approve_their_own_request(self, own_request, owner):
        with pytest.raises(SelfApprovalNotAllowed):
            approve_gate_out(own_request, actor=owner)

        assert own_request.status == GateOutStatus.PENDING_APPROVAL

    def test_a_requester_cannot_reject_their_own_request_either(self, own_request, owner):
        """Otherwise the guard is trivially bypassed: reject, amend, resubmit."""
        with pytest.raises(SelfApprovalNotAllowed):
            reject_gate_out(own_request, actor=owner, reason="Changed my mind")

    def test_it_is_permitted_once_the_setting_is_enabled(self, own_request, owner, tenant):
        """A one-person contractor genuinely needs this, so it is configurable —
        but off by default (F3)."""
        settings = tenant.settings
        settings.allow_self_approval = True
        settings.save()

        approve_gate_out(own_request, actor=owner)

        assert own_request.status == GateOutStatus.APPROVED

    def test_another_approver_can_approve_it(self, own_request, tenant, owner_role):
        second = UserFactory(organization=tenant, full_name="Second Approver")
        UserRoleFactory(user=second, role=owner_role)

        approve_gate_out(own_request, actor=second)

        assert own_request.status == GateOutStatus.APPROVED


class TestDelegatedApproval:
    """F5, T4.9: "X on behalf of Y", never as Y."""

    def test_a_delegate_can_approve(self, tenant, yard, storekeeper, technician, owner_role, owner):
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 1)
        submit_gate_out(gate_out, submitted_by=storekeeper)

        deputy = UserFactory(organization=tenant, full_name="Dan Deputy")
        DelegationFactory(from_user=owner, to_user=deputy, role=owner_role)

        approve_gate_out(gate_out, actor=deputy)

        assert gate_out.status == GateOutStatus.APPROVED

    def test_the_action_is_attributed_to_both(
        self, tenant, yard, storekeeper, technician, owner_role, owner
    ):
        """Attributing it to the principal alone would forge their signature on a
        decision they never made."""
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 1)
        submit_gate_out(gate_out, submitted_by=storekeeper)

        deputy = UserFactory(organization=tenant, full_name="Dan Deputy")
        delegation = DelegationFactory(from_user=owner, to_user=deputy, role=owner_role)

        approve_gate_out(gate_out, actor=deputy)

        action = ApprovalAction.objects.get(decision=ApprovalDecision.APPROVED)
        assert action.actor == deputy
        assert action.on_behalf_of == owner
        assert action.delegation == delegation
        assert action.attribution == "Dan Deputy on behalf of Sam Owner"

    def test_an_expired_delegation_does_not_confer_approval(
        self, tenant, yard, storekeeper, technician, owner_role, owner
    ):
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 1)
        submit_gate_out(gate_out, submitted_by=storekeeper)

        deputy = UserFactory(organization=tenant, full_name="Dan Deputy")
        DelegationFactory(
            from_user=owner,
            to_user=deputy,
            role=owner_role,
            starts_at=timezone.now() - timedelta(days=10),
            ends_at=timezone.now() - timedelta(days=1),
        )

        with pytest.raises(NotAnApprover):
            approve_gate_out(gate_out, actor=deputy)


# --------------------------------------------------------------------------
# T4.13 — release
# --------------------------------------------------------------------------


class TestReleaseRefusals:
    """G1: the most important refusal in the system."""

    def test_an_unapproved_pass_cannot_be_released(
        self, tenant, yard, storekeeper, technician, owner_role
    ):
        """If this can be bypassed, nothing else in the design matters."""
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)
        submit_gate_out(gate_out, submitted_by=storekeeper)

        with pytest.raises(ReleaseNotPermitted, match="Only an approved pass"):
            release_gate_out(gate_out, released_by=storekeeper)

        assert balance_at(yard.node, item) == Decimal("10")

    def test_a_draft_cannot_be_released(self, tenant, yard, storekeeper, technician):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)

        with pytest.raises(ReleaseNotPermitted):
            release_gate_out(gate_out, released_by=storekeeper)

    def test_an_expired_pass_cannot_be_released(self, tenant, yard, storekeeper, technician, owner):
        """Q3: the stock an expired approval was checked against has moved on, so
        releasing against it would authorise something nobody reviewed."""
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)
        submit_gate_out(gate_out, submitted_by=storekeeper)

        gate_out.expires_at = timezone.now() - timedelta(hours=1)
        gate_out.save(update_fields=["expires_at"])

        with pytest.raises(ReleaseNotPermitted, match="expired"):
            release_gate_out(gate_out, released_by=storekeeper)

    def test_a_cancelled_pass_cannot_be_released(self, tenant, yard, storekeeper, technician):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        cancel_gate_out(gate_out, reason="No longer needed", cancelled_by=storekeeper)

        with pytest.raises(ReleaseNotPermitted):
            release_gate_out(gate_out, released_by=storekeeper)


class TestRelease:
    @pytest.fixture
    def approved(self, tenant, yard, storekeeper, technician):
        item = ItemTypeFactory(name="Jumper 1/2 inch 3m")
        stock_in(tenant, yard.node, item, 100)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 20)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        return gate_out, item

    def test_releasing_moves_stock_into_custody(
        self, approved, tenant, storekeeper, technician, yard
    ):
        """I1: custody is created on release to a named person."""
        gate_out, item = approved

        release_gate_out(
            gate_out, released_by=storekeeper, vehicle_reg="KDA 123X", driver_name="Tom"
        )

        assert gate_out.status == GateOutStatus.RELEASED
        assert balance_at(yard.node, item) == Decimal("80")
        assert balance_at(node_for_user(technician), item) == Decimal("20")

    def test_the_vehicle_and_driver_are_recorded(self, approved, storekeeper):
        """G2: so the load is attributable."""
        gate_out, _item = approved

        release_gate_out(
            gate_out, released_by=storekeeper, vehicle_reg="KDA 123X", driver_name="Tom Driver"
        )

        assert gate_out.vehicle_reg == "KDA 123X"
        assert gate_out.driver_name == "Tom Driver"
        assert gate_out.released_by == storekeeper
        assert gate_out.released_at is not None

    def test_a_partial_release_leaves_the_pass_open(self, approved, storekeeper, yard):
        """F7: "a partially released gate-out remains open until fully released"."""
        gate_out, _item = approved
        line = gate_out.lines.get()

        release_gate_out(gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("8")})

        assert gate_out.status == GateOutStatus.PARTIALLY_RELEASED
        line.refresh_from_db()
        assert line.released_qty == Decimal("8")
        assert line.outstanding_qty == Decimal("12")

    def test_the_rest_can_be_released_later(self, approved, storekeeper, technician):
        gate_out, item = approved
        line = gate_out.lines.get()

        release_gate_out(gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("8")})
        release_gate_out(gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("12")})

        assert gate_out.status == GateOutStatus.RELEASED
        assert balance_at(node_for_user(technician), item) == Decimal("20")

    def test_releasing_more_than_approved_is_refused(self, approved, storekeeper):
        """The over-release F7's constraint exists to prevent."""
        gate_out, _item = approved
        line = gate_out.lines.get()

        with pytest.raises(ReleaseNotPermitted, match="remains on this pass"):
            release_gate_out(
                gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("25")}
            )

    def test_a_signature_can_be_made_mandatory(self, approved, storekeeper, tenant):
        """G3: optional by default; an admin may make it mandatory."""
        settings = tenant.settings
        settings.signature_required_on_release = True
        settings.save()
        gate_out, _item = approved

        with pytest.raises(GateOutNotReady, match="signature"):
            release_gate_out(gate_out, released_by=storekeeper)


class TestReleaseVariance:
    """G1's edge case: a short load is recorded, not refused."""

    @pytest.fixture
    def approved(self, tenant, yard, storekeeper, technician):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 100)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 20)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        return gate_out, item

    def test_a_short_release_records_a_variance(self, approved, storekeeper):
        """T4.14: "releasing a load with one short line records the variance and
        completes the release"."""
        gate_out, _item = approved
        line = gate_out.lines.get()

        release_gate_out(
            gate_out,
            released_by=storekeeper,
            released_lines={line.pk: Decimal("18")},
            variance_reasons={line.pk: "Two damaged in the racking"},
        )

        variance = ReleaseVariance.objects.get()
        assert variance.approved_qty == Decimal("20")
        assert variance.released_qty == Decimal("18")
        assert variance.reason == "Two damaged in the racking"
        assert variance.is_open is True

    def test_the_release_still_completes(self, approved, storekeeper, technician):
        """It "blocks nothing at the gate" — stranding a job over a paperwork
        mismatch would teach people to avoid the system."""
        gate_out, item = approved
        line = gate_out.lines.get()

        release_gate_out(gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("18")})

        assert balance_at(node_for_user(technician), item) == Decimal("18")
        assert gate_out.status == GateOutStatus.PARTIALLY_RELEASED

    def test_a_variance_stays_open_until_acknowledged(self, approved, storekeeper, owner):
        gate_out, _item = approved
        line = gate_out.lines.get()
        release_gate_out(gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("18")})
        variance = ReleaseVariance.objects.get()

        acknowledge_variance(variance, actor=owner)

        variance.refresh_from_db()
        assert variance.is_open is False
        assert variance.acknowledged_by == owner

    def test_acknowledging_twice_is_refused(self, approved, storekeeper, owner):
        gate_out, _item = approved
        line = gate_out.lines.get()
        release_gate_out(gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("18")})
        variance = ReleaseVariance.objects.get()
        acknowledge_variance(variance, actor=owner)

        with pytest.raises(GateOutNotReady, match="already been acknowledged"):
            acknowledge_variance(variance, actor=owner)


# --------------------------------------------------------------------------
# T4.11, T4.12 — amend and cancel
# --------------------------------------------------------------------------


class TestAmend:
    """F6: amending an approved pass voids the approval and re-routes."""

    def test_amending_an_approved_pass_voids_its_approval(
        self, tenant, yard, storekeeper, technician, owner_role, owner
    ):
        """The rule that stops approval being decorative.

        Without it, someone could get a small request approved and then enlarge
        it before release.
        """
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 1)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        approve_gate_out(gate_out, actor=owner)
        assert gate_out.status == GateOutStatus.APPROVED

        amend_gate_out(gate_out, amended_by=storekeeper)

        assert gate_out.status == GateOutStatus.PENDING_APPROVAL
        assert gate_out.approved_at is None
        assert gate_out.expires_at is None

    def test_the_voided_approval_is_retained_not_deleted(
        self, tenant, yard, storekeeper, technician, owner_role, owner
    ):
        """T4.11: "the voided approval is retained, not deleted"."""
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 1)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        approve_gate_out(gate_out, actor=owner)

        amend_gate_out(gate_out, amended_by=storekeeper)

        assert ApprovalAction.objects.filter(decision=ApprovalDecision.APPROVED).exists()
        assert ApprovalRequest.objects.filter(status=ApprovalRequestStatus.SUPERSEDED).exists()

    def test_the_version_is_incremented(
        self, tenant, yard, storekeeper, technician, owner_role, owner
    ):
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 1)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        approve_gate_out(gate_out, actor=owner)

        amend_gate_out(gate_out, amended_by=storekeeper)

        assert gate_out.version == 2

    def test_a_partially_released_pass_cannot_be_amended(
        self, tenant, yard, storekeeper, technician
    ):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 100)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        line = add_line(gate_out, item, 20)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        release_gate_out(gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("5")})

        # Refused on status: a partially released pass is past the point where
        # amending it would mean anything. Close it and raise a new one.
        with pytest.raises(GateOutNotReady, match="cannot be amended"):
            amend_gate_out(gate_out, amended_by=storekeeper)


class TestCancel:
    """F8: a reason is required, and never after a release."""

    def test_cancelling_requires_a_reason(self, tenant, yard, storekeeper, technician):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)

        with pytest.raises(GateOutNotReady, match="requires a reason"):
            cancel_gate_out(gate_out, reason="", cancelled_by=storekeeper)

    def test_cancelling_a_draft_works(self, tenant, yard, storekeeper, technician):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)

        cancel_gate_out(gate_out, reason="Job postponed", cancelled_by=storekeeper)

        assert gate_out.status == GateOutStatus.CANCELLED
        assert gate_out.cancel_reason == "Job postponed"

    def test_cancelling_after_a_release_is_refused(self, tenant, yard, storekeeper, technician):
        """T4.12's criterion. Cancelling the paperwork after material has left
        would leave stock unaccounted for."""
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 100)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        line = add_line(gate_out, item, 20)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        release_gate_out(gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("5")})

        with pytest.raises(GateOutNotReady, match="already left the yard"):
            cancel_gate_out(gate_out, reason="Changed our minds", cancelled_by=storekeeper)

    def test_a_partially_released_pass_is_closed_with_a_reason_instead(
        self, tenant, yard, storekeeper, technician
    ):
        """F7: the outstanding balance is written off, so it needs an explanation."""
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 100)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        line = add_line(gate_out, item, 20)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        release_gate_out(gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("5")})

        close_gate_out(gate_out, reason="Job finished with what was taken", closed_by=storekeeper)

        assert gate_out.status == GateOutStatus.CLOSED
        assert gate_out.close_reason == "Job finished with what was taken"

    def test_closing_a_partial_pass_without_a_reason_is_refused(
        self, tenant, yard, storekeeper, technician
    ):
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 100)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        line = add_line(gate_out, item, 20)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        release_gate_out(gate_out, released_by=storekeeper, released_lines={line.pk: Decimal("5")})

        with pytest.raises(GateOutNotReady, match="requires a reason"):
            close_gate_out(gate_out, reason="", closed_by=storekeeper)


# --------------------------------------------------------------------------
# T4.10 — escalation and expiry
# --------------------------------------------------------------------------


class TestExpiryAndEscalation:
    def test_a_stale_approved_pass_expires(self, tenant, yard, storekeeper, technician):
        """Q3, T4.10."""
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)
        submit_gate_out(gate_out, submitted_by=storekeeper)

        gate_out.expires_at = timezone.now() - timedelta(minutes=1)
        gate_out.save(update_fields=["expires_at"])

        assert expire_stale_passes(tenant.pk) == 1

        gate_out.refresh_from_db()
        assert gate_out.status == GateOutStatus.EXPIRED

    def test_expiry_fires_once_not_once_per_run(self, tenant, yard, storekeeper, technician):
        """T4.10: "both transitions fire once"."""
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        gate_out.expires_at = timezone.now() - timedelta(minutes=1)
        gate_out.save(update_fields=["expires_at"])

        assert expire_stale_passes(tenant.pk) == 1
        assert expire_stale_passes(tenant.pk) == 0

    def test_an_expired_pass_can_be_resubmitted(self, tenant, yard, storekeeper, technician):
        """A job still needs doing; the approval simply has to be fresh."""
        item = ItemTypeFactory()
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 2)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        gate_out.expires_at = timezone.now() - timedelta(minutes=1)
        gate_out.save(update_fields=["expires_at"])
        expire_stale_passes(tenant.pk)
        gate_out.refresh_from_db()

        submit_gate_out(gate_out, submitted_by=storekeeper)

        assert gate_out.status == GateOutStatus.APPROVED

    def test_an_unanswered_approval_escalates(
        self, tenant, yard, storekeeper, technician, owner_role
    ):
        """F5: "unanswered after N hours, escalate to a named fallback"."""
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 1)
        submit_gate_out(gate_out, submitted_by=storekeeper)

        ApprovalRequest.objects.filter(document_id=str(gate_out.pk)).update(
            due_at=timezone.now() - timedelta(hours=1)
        )

        assert escalate_overdue_approvals(tenant.pk) == 1

        assert (
            ApprovalRequest.objects.get(document_id=str(gate_out.pk)).status
            == ApprovalRequestStatus.ESCALATED
        )

    def test_escalation_fires_once_not_once_per_run(
        self, tenant, yard, storekeeper, technician, owner_role
    ):
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 1)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        ApprovalRequest.objects.filter(document_id=str(gate_out.pk)).update(
            due_at=timezone.now() - timedelta(hours=1)
        )

        assert escalate_overdue_approvals(tenant.pk) == 1
        assert escalate_overdue_approvals(tenant.pk) == 0

    def test_an_escalated_request_can_still_be_approved(
        self, tenant, yard, storekeeper, technician, owner_role, owner
    ):
        """Escalation raises the alarm; it does not block the decision."""
        rule_for(Criticality.HIGH, owner_role, sequence=3)
        item = ItemTypeFactory(category=ItemCategoryFactory(criticality=Criticality.HIGH))
        stock_in(tenant, yard.node, item, 10)
        gate_out = make_gate_out(tenant, yard, storekeeper, technician)
        add_line(gate_out, item, 1)
        submit_gate_out(gate_out, submitted_by=storekeeper)
        ApprovalRequest.objects.filter(document_id=str(gate_out.pk)).update(
            due_at=timezone.now() - timedelta(hours=1)
        )
        escalate_overdue_approvals(tenant.pk)

        approve_gate_out(gate_out, actor=owner)

        assert gate_out.status == GateOutStatus.APPROVED
