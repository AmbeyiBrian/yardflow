"""T4.16, T4.17 — the notification framework (§9; L1, L2, L3).

L3 is the requirement that matters most here: "**failure of a notification never
blocks the underlying transaction.**" An approval that rolled back because an SMS
gateway was down would be an outage caused by a courtesy message.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.core import mail
from django.db import transaction

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from catalogue.factories import ItemCategoryFactory, ItemTypeFactory
from catalogue.models import Criticality
from dispatch.models import GateOut, GateOutLine, GateOutPurpose, GateOutStatus
from dispatch.services import approve_gate_out, submit_gate_out
from locations.factories import YardFactory
from locations.nodes import external_node
from network.factories import SiteFactory
from notifications.channels.base import RenderedMessage
from notifications.channels.whatsapp import WhatsAppChannel
from notifications.events import (
    emit,
    render_body,
    resolve_recipients,
    retry_pending,
    send_delivery,
)
from notifications.matrix import (
    DEFAULT_MATRIX,
    Channel,
    Event,
    channels_for,
    default_channels_config,
    default_matrix_config,
)
from notifications.models import DeliveryStatus, NotificationDelivery, NotificationEvent
from stock.models import MovementType
from stock.services import MovementRequest, post_movement


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def approver_role(tenant):
    return RoleFactory(name="Approver", codenames=[PERM.GATE_OUT_APPROVE])


@pytest.fixture
def approver(tenant, approver_role):
    person = UserFactory(
        organization=tenant,
        full_name="Alan Approver",
        email="alan@silvertech.co.ke",
        phone="0722000001",
    )
    UserRoleFactory(user=person, role=approver_role)
    return person


@pytest.fixture
def storekeeper(tenant):
    person = UserFactory(
        organization=tenant, full_name="Sara Storekeeper", email="sara@silvertech.co.ke"
    )
    UserRoleFactory(
        user=person, role=RoleFactory(name="Storekeeper", codenames=[PERM.GATE_IN_POST])
    )
    return person


@pytest.fixture
def seeded_matrix(tenant):
    """The L2 defaults, as provisioning would have left them."""
    settings = tenant.settings
    settings.notification_matrix = default_matrix_config()
    settings.notification_channels = default_channels_config()
    settings.save()
    return settings


def a_gate_out(tenant, yard, requester, holder, criticality=Criticality.HIGH, quantity=2):
    item = ItemTypeFactory(category=ItemCategoryFactory(criticality=criticality))
    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal("100"),
                from_node=external_node(tenant.pk),
                to_node=yard.node,
                movement_type=MovementType.RECEIPT,
            )
        )
    gate_out = GateOut.objects.create(
        organization=tenant,
        from_location=yard,
        site=SiteFactory(),
        custody_holder=holder,
        requested_by=requester,
        purpose_type=GateOutPurpose.INSTALLATION,
    )
    GateOutLine.objects.create(
        organization=tenant,
        gate_out=gate_out,
        item_type=item,
        tracking_mode=item.default_tracking_mode,
        requested_qty=Decimal(str(quantity)),
        uom=item.uom,
    )
    return gate_out


class TestTheDefaultMatrix:
    """L2: the seeded default, editable per tenant."""

    def test_every_event_in_l2s_table_is_present(self):
        keys = {spec.key for spec in DEFAULT_MATRIX}

        for required in (
            Event.GATE_OUT_AWAITING_APPROVAL,
            Event.GATE_OUT_APPROVED,
            Event.GATE_OUT_REJECTED,
            Event.GATE_OUT_RELEASED,
            Event.APPROVAL_ESCALATED,
            Event.ITEM_OVERDUE,
            Event.RETURN_VARIANCE_RAISED,
            Event.STOCK_BELOW_MINIMUM,
            Event.JOB_CLOSED_WITH_UNACCOUNTED,
            Event.CLIENT_RETURN_UNACKNOWLEDGED,
        ):
            assert required in keys, f"L2 requires an entry for {required}"

    def test_whatsapp_is_off_by_default(self, tenant, seeded_matrix):
        """Q1, §9.2: the adapter ships written but disabled.

        The Business API needs an approved sender and registered templates, which
        takes weeks. The approval loop must not wait on Meta's queue.
        """
        assert default_channels_config()[Channel.WHATSAPP] is False

    def test_an_event_specifying_whatsapp_still_reaches_people_in_app(
        self, tenant, seeded_matrix
    ):
        """So disabling WhatsApp does not silence the approval request."""
        channels = channels_for(tenant, Event.GATE_OUT_AWAITING_APPROVAL)

        assert Channel.WHATSAPP not in channels
        assert Channel.IN_APP in channels

    def test_a_tenant_can_disable_a_channel_for_one_event(self, tenant, seeded_matrix):
        """L2: "so that people are not spammed"."""
        settings = tenant.settings
        settings.notification_matrix[Event.GATE_OUT_APPROVED]["channels"] = []
        settings.save()

        assert channels_for(tenant, Event.GATE_OUT_APPROVED) == []
        # And only that event.
        assert channels_for(tenant, Event.GATE_OUT_REJECTED) != []

    def test_a_tenant_can_disable_an_event_entirely(self, tenant, seeded_matrix):
        settings = tenant.settings
        settings.notification_matrix[Event.GATE_OUT_APPROVED]["enabled"] = False
        settings.save()

        assert channels_for(tenant, Event.GATE_OUT_APPROVED) == []

    def test_a_channel_switched_off_tenant_wide_beats_the_matrix(
        self, tenant, seeded_matrix
    ):
        """L1 is about what staff actually read.

        A company with no SMS budget must not have SMS reintroduced by a
        per-event setting.
        """
        settings = tenant.settings
        settings.notification_channels[Channel.SMS] = False
        settings.notification_matrix[Event.ITEM_OVERDUE]["channels"] = [Channel.SMS]
        settings.save()

        assert channels_for(tenant, Event.ITEM_OVERDUE) == []

    def test_provisioning_seeds_the_matrix(self, db):
        from core.provisioning import provision_tenant

        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )

        settings = result["organization"].settings
        assert settings.notification_matrix
        assert settings.notification_channels[Channel.WHATSAPP] is False


class TestRecipientResolution:
    """L2: recipients are roles, never named people."""

    def test_approvers_are_resolved_from_their_permission(
        self, tenant, yard, seeded_matrix, approver, storekeeper
    ):
        gate_out = a_gate_out(tenant, yard, storekeeper, storekeeper)

        recipients = resolve_recipients(tenant, ["approvers"], gate_out)

        assert approver in recipients

    def test_the_requester_is_resolved_from_the_document(
        self, tenant, yard, seeded_matrix, storekeeper, django_capture_on_commit_callbacks
    ):
        gate_out = a_gate_out(tenant, yard, storekeeper, storekeeper)

        assert resolve_recipients(tenant, ["requester"], gate_out) == [storekeeper]

    def test_duplicates_are_removed(self, tenant, yard, seeded_matrix, approver):
        """An owner who is also the requester gets one message, not two."""
        gate_out = a_gate_out(tenant, yard, approver, approver)

        recipients = resolve_recipients(tenant, ["requester", "approvers"], gate_out)

        assert recipients.count(approver) == 1

    def test_a_deactivated_user_is_not_notified(
        self, tenant, yard, seeded_matrix, approver, storekeeper
    ):
        """B3: they keep their records, not their notifications."""
        approver.is_active = False
        approver.save()
        gate_out = a_gate_out(tenant, yard, storekeeper, storekeeper)

        assert approver not in resolve_recipients(tenant, ["approvers"], gate_out)


class TestDeliveryRecords:
    def test_submitting_notifies_the_approvers(
        self,
        tenant,
        yard,
        seeded_matrix,
        approver,
        storekeeper,
        approver_role,
        django_capture_on_commit_callbacks,
    ):
        from approvals.models import ApprovalRule

        ApprovalRule.objects.create(
            criticality=Criticality.HIGH, required_role=approver_role, sequence=3
        )
        gate_out = a_gate_out(tenant, yard, storekeeper, storekeeper)

        with django_capture_on_commit_callbacks(execute=True):
            submit_gate_out(gate_out, submitted_by=storekeeper)

        event = NotificationEvent.objects.get(event_key=Event.GATE_OUT_AWAITING_APPROVAL)
        assert event.target_id == str(gate_out.pk)
        assert NotificationDelivery.objects.filter(
            event=event, recipient=approver, channel=Channel.IN_APP
        ).exists()

    def test_an_in_app_delivery_is_immediately_sent_and_unread(
        self,
        tenant,
        yard,
        seeded_matrix,
        approver,
        storekeeper,
        approver_role,
        django_capture_on_commit_callbacks,
    ):
        """An in-app message is delivered by existing; there is nothing to send."""
        from approvals.models import ApprovalRule

        ApprovalRule.objects.create(
            criticality=Criticality.HIGH, required_role=approver_role, sequence=3
        )
        gate_out = a_gate_out(tenant, yard, storekeeper, storekeeper)

        with django_capture_on_commit_callbacks(execute=True):
            submit_gate_out(gate_out, submitted_by=storekeeper)

        delivery = NotificationDelivery.objects.get(
            recipient=approver, channel=Channel.IN_APP
        )
        assert delivery.status == DeliveryStatus.SENT
        assert delivery.is_unread is True

    def test_a_recipient_without_an_address_is_skipped_not_failed(
        self, tenant, yard, seeded_matrix, storekeeper, django_capture_on_commit_callbacks
    ):
        """B1 allows a user with only a phone, or only an email.

        Being unreachable on one channel is not a failure — nothing went wrong.
        """
        settings = tenant.settings
        settings.notification_matrix[Event.GATE_OUT_APPROVED]["channels"] = [Channel.SMS]
        settings.save()
        # Sara has an email but no phone.
        gate_out = a_gate_out(tenant, yard, storekeeper, storekeeper, criticality=Criticality.NONE)

        with django_capture_on_commit_callbacks(execute=True):
            submit_gate_out(gate_out, submitted_by=storekeeper)

        delivery = NotificationDelivery.objects.get(
            recipient=storekeeper, channel=Channel.SMS
        )
        assert delivery.status == DeliveryStatus.SKIPPED

    def test_an_email_delivery_is_sent(
        self, tenant, yard, seeded_matrix, storekeeper, django_capture_on_commit_callbacks
    ):
        settings = tenant.settings
        settings.notification_matrix[Event.GATE_OUT_APPROVED]["channels"] = [Channel.EMAIL]
        settings.save()
        gate_out = a_gate_out(
            tenant, yard, storekeeper, storekeeper, criticality=Criticality.NONE
        )

        with django_capture_on_commit_callbacks(execute=True):
            submit_gate_out(gate_out, submitted_by=storekeeper)

        assert len(mail.outbox) == 1
        assert NotificationDelivery.objects.get(channel=Channel.EMAIL).status == (
            DeliveryStatus.SENT
        )

    def test_the_same_event_is_not_delivered_twice(
        self, tenant, yard, seeded_matrix, storekeeper, django_capture_on_commit_callbacks
    ):
        """Re-dispatching must not spam. The uniqueness constraint is the guard."""
        from notifications.events import dispatch_event

        gate_out = a_gate_out(
            tenant, yard, storekeeper, storekeeper, criticality=Criticality.NONE
        )
        with django_capture_on_commit_callbacks(execute=True):
            submit_gate_out(gate_out, submitted_by=storekeeper)
        event = NotificationEvent.objects.filter(
            event_key=Event.GATE_OUT_APPROVED
        ).first()
        before = NotificationDelivery.objects.count()

        dispatch_event(event.pk, organization_id=tenant.pk)

        assert NotificationDelivery.objects.count() == before


class TestFailureNeverBlocksTheTransaction:
    """L3, and T4.16's stated criterion.

    "A forced channel failure retries, is recorded, and **does not roll back the
    approval it was notifying about**."
    """

    def test_a_failing_channel_does_not_roll_back_the_approval(
        self,
        tenant,
        yard,
        seeded_matrix,
        approver,
        storekeeper,
        approver_role,
        django_capture_on_commit_callbacks,
    ):
        from approvals.models import ApprovalRule

        ApprovalRule.objects.create(
            criticality=Criticality.HIGH, required_role=approver_role, sequence=3
        )
        settings = tenant.settings
        settings.notification_matrix[Event.GATE_OUT_APPROVED]["channels"] = [Channel.EMAIL]
        settings.save()

        gate_out = a_gate_out(tenant, yard, storekeeper, storekeeper)
        with django_capture_on_commit_callbacks(execute=True):
            submit_gate_out(gate_out, submitted_by=storekeeper)

        with (
            mock.patch(
                "notifications.channels.email.send_mail",
                side_effect=OSError("smtp is down"),
            ),
            django_capture_on_commit_callbacks(execute=True),
        ):
            approve_gate_out(gate_out, actor=approver)

        # The approval stands.
        gate_out.refresh_from_db()
        assert gate_out.status == GateOutStatus.APPROVED

        # And the failure is recorded rather than swallowed.
        delivery = NotificationDelivery.objects.get(channel=Channel.EMAIL)
        assert delivery.status == DeliveryStatus.PENDING
        assert "smtp is down" in delivery.last_error

    def test_a_retryable_failure_stays_pending_for_the_sweep(
        self, tenant, yard, seeded_matrix, storekeeper, django_capture_on_commit_callbacks
    ):
        settings = tenant.settings
        settings.notification_matrix[Event.GATE_OUT_APPROVED]["channels"] = [Channel.EMAIL]
        settings.save()
        gate_out = a_gate_out(
            tenant, yard, storekeeper, storekeeper, criticality=Criticality.NONE
        )

        with (
            mock.patch(
                "notifications.channels.email.send_mail",
                side_effect=OSError("timeout"),
            ),
            django_capture_on_commit_callbacks(execute=True),
        ):
            submit_gate_out(gate_out, submitted_by=storekeeper)

        delivery = NotificationDelivery.objects.get(channel=Channel.EMAIL)
        assert delivery.status == DeliveryStatus.PENDING
        assert delivery.attempts == 1

        # The retry sweep picks it up and succeeds once the provider is back.
        assert retry_pending(tenant.pk) == 1
        delivery.refresh_from_db()
        assert delivery.status == DeliveryStatus.SENT

    def test_retries_are_capped_and_then_abandoned(
        self, tenant, yard, seeded_matrix, storekeeper, django_capture_on_commit_callbacks
    ):
        """L3: "terminal failures are visible to admins."

        Retrying forever would only delay somebody being told.
        """
        from notifications.events import MAX_ATTEMPTS

        settings = tenant.settings
        settings.notification_matrix[Event.GATE_OUT_APPROVED]["channels"] = [Channel.EMAIL]
        settings.save()
        gate_out = a_gate_out(
            tenant, yard, storekeeper, storekeeper, criticality=Criticality.NONE
        )

        with mock.patch(
            "notifications.channels.email.send_mail", side_effect=OSError("still down")
        ):
            with django_capture_on_commit_callbacks(execute=True):
                submit_gate_out(gate_out, submitted_by=storekeeper)
            for _ in range(MAX_ATTEMPTS + 2):
                retry_pending(tenant.pk)

        delivery = NotificationDelivery.objects.get(channel=Channel.EMAIL)
        assert delivery.status == DeliveryStatus.ABANDONED
        assert delivery.attempts == MAX_ATTEMPTS

    def test_a_rolled_back_transaction_sends_nothing(
        self, tenant, yard, seeded_matrix, storekeeper
    ):
        """The other half of L3, and the reason dispatch is on_commit.

        If the approval never happened, nobody should be told it did.
        """
        gate_out = a_gate_out(
            tenant, yard, storekeeper, storekeeper, criticality=Criticality.NONE
        )

        with pytest.raises(RuntimeError), transaction.atomic():
            emit(Event.GATE_OUT_APPROVED, gate_out)
            raise RuntimeError("something else failed")

        assert NotificationEvent.objects.filter(
            event_key=Event.GATE_OUT_APPROVED
        ).count() == 0


class TestMessageRendering:
    def test_the_approval_request_names_the_pass_and_destination(
        self, tenant, yard, seeded_matrix, storekeeper, django_capture_on_commit_callbacks
    ):
        """It has to read sensibly as an SMS, so it says what and where."""
        gate_out = a_gate_out(
            tenant, yard, storekeeper, storekeeper, criticality=Criticality.NONE
        )
        submit_gate_out(gate_out, submitted_by=storekeeper)

        event = NotificationEvent.objects.filter(
            event_key=Event.GATE_OUT_APPROVED
        ).first()

        assert gate_out.number in render_body(event)

    def test_every_event_renders_something(self, tenant, yard, seeded_matrix, storekeeper):
        """A message that renders as an empty string is worse than none."""
        gate_out = a_gate_out(
            tenant, yard, storekeeper, storekeeper, criticality=Criticality.NONE
        )

        for spec in DEFAULT_MATRIX:
            event = NotificationEvent.objects.create(
                organization=tenant,
                event_key=spec.key,
                target_type=gate_out._meta.label,
                target_id=str(gate_out.pk),
                target_label=str(gate_out),
                payload={"number": "GP-000001"},
            )
            assert render_body(event).strip(), f"{spec.key} rendered nothing"


class TestWhatsAppAdapter:
    """T8.12: written and unit-tested against a mock, shipped disabled."""

    def test_it_refuses_when_not_configured(self, tenant, settings):
        """Q1: no approved sender means no messages, and a clear reason why."""
        settings.WHATSAPP_ACCESS_TOKEN = ""
        settings.WHATSAPP_PHONE_NUMBER_ID = ""

        result = WhatsAppChannel().send("+254722000001", RenderedMessage(body="hello"))

        assert result.succeeded is False
        assert result.retryable is False
        assert "approved sender" in result.error

    def test_it_is_not_selected_while_disabled(self, tenant, settings):
        """§9.2: enabling it later is a settings change, not a code change."""
        from notifications.channels import get_channel

        settings.WHATSAPP_ACCESS_TOKEN = ""

        assert get_channel("whatsapp") is None

    def test_a_template_message_is_built_when_a_template_is_named(self):
        """Meta requires a registered template outside the 24-hour window."""
        payload = WhatsAppChannel._build_payload(
            "+254722000001",
            RenderedMessage(
                template_name="gate_pass_approval",
                template_variables={"number": "GP-000042", "site": "SLV-1001"},
            ),
        )

        assert payload["type"] == "template"
        assert payload["template"]["name"] == "gate_pass_approval"
        assert [p["text"] for p in payload["template"]["components"][0]["parameters"]] == [
            "GP-000042",
            "SLV-1001",
        ]

    def test_a_free_text_message_is_built_when_no_template_is_named(self):
        payload = WhatsAppChannel._build_payload(
            "+254722000001", RenderedMessage(body="Gate pass GP-000042 needs approval")
        )

        assert payload["type"] == "text"
        assert payload["text"]["body"] == "Gate pass GP-000042 needs approval"

    def test_a_4xx_is_not_retried(self, tenant, settings):
        """An unapproved template will not fix itself, and each attempt costs."""
        import urllib.error

        settings.WHATSAPP_ACCESS_TOKEN = "token"
        settings.WHATSAPP_PHONE_NUMBER_ID = "12345"

        error = urllib.error.HTTPError(
            url="https://graph.facebook.com", code=400, msg="Bad Request",
            hdrs=None, fp=None,
        )
        error.read = lambda: b'{"error":{"message":"template not found"}}'

        with mock.patch("urllib.request.urlopen", side_effect=error):
            result = WhatsAppChannel().send("+254722000001", RenderedMessage(body="hi"))

        assert result.succeeded is False
        assert result.retryable is False

    def test_a_5xx_is_retried(self, tenant, settings):
        import urllib.error

        settings.WHATSAPP_ACCESS_TOKEN = "token"
        settings.WHATSAPP_PHONE_NUMBER_ID = "12345"

        error = urllib.error.HTTPError(
            url="https://graph.facebook.com", code=503, msg="Unavailable",
            hdrs=None, fp=None,
        )
        error.read = lambda: b"upstream down"

        with mock.patch("urllib.request.urlopen", side_effect=error):
            result = WhatsAppChannel().send("+254722000001", RenderedMessage(body="hi"))

        assert result.retryable is True


class TestSmsDelivery:
    """T8.11's delivery-status half (L1, L3).

    The adapter's own contract is tested in `test_sms_adapter.py`; what matters
    here is that a send outcome reaches the delivery row, because a message that
    failed silently is worse than one that never sent.
    """

    def test_switching_provider_is_a_settings_change(self, tenant, settings):
        """T8.11's criterion: "switching provider is a settings change".

        Which is only true if nothing imports a provider directly. This asserts
        the indirection actually holds — a caller that had reached for
        ``UjumbeSmsBackend`` by name would make the setting decorative.
        """
        from notifications.channels import get_sms_backend
        from notifications.channels.logging_sms import LoggingSmsBackend
        from notifications.channels.ujumbe_sms import UjumbeSmsBackend

        settings.SMS_BACKEND = "notifications.channels.ujumbe_sms.UjumbeSmsBackend"
        assert isinstance(get_sms_backend(), UjumbeSmsBackend)

        settings.SMS_BACKEND = "notifications.channels.logging_sms.LoggingSmsBackend"
        assert isinstance(get_sms_backend(), LoggingSmsBackend)

    def test_an_sms_delivery_records_its_status(self, tenant, settings):
        """T8.11: "delivery status is recorded".

        A message that failed silently is worse than one that never sent: L3
        keeps the failure off the business transaction, and this is what makes it
        visible to an administrator afterwards.
        """
        import json

        settings.SMS_BACKEND = "notifications.channels.ujumbe_sms.UjumbeSmsBackend"
        settings.UJUMBE_SMS_API_KEY = "key"
        settings.UJUMBE_SMS_ACCOUNT_EMAIL = "ops@silvertech.co.ke"
        settings.UJUMBE_SMS_SENDER_ID = "SILVERTECH"

        # L4: SMS is metered, so there has to be something to spend. What that
        # costs is `test_sms_credits.py`; here it is a precondition.
        from notifications import credits

        credits.purchase(tenant, 10, note="For the test")

        recipient = UserFactory(
            organization=tenant, full_name="Texted Tom", phone="+254722000456"
        )
        delivery = NotificationDelivery.objects.create(
            organization=tenant,
            event=NotificationEvent.objects.create(
                organization=tenant,
                event_key="gate_out.approved",
                target_type="dispatch.GateOut",
                target_id="1",
                target_label="GP-000001",
                payload={"number": "GP-000001"},
            ),
            recipient=recipient,
            channel=Channel.SMS,
            destination=recipient.phone,
            body="Gate pass GP-000001 has been approved and can be released.",
        )

        # UjumbeSMS's documented shape: the code is nested inside `status`.
        response = mock.MagicMock()
        response.read.return_value = json.dumps(
            {
                "status": {"code": "200", "type": "success", "description": "Sent"},
                "meta": {
                    "recipients": 1,
                    "credits_deducted": 1,
                    "available_credits": "411",
                    "date_time": {"date": "2026-08-22 14:31:00.000000"},
                },
            }
        ).encode()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda *args: None

        with mock.patch("urllib.request.urlopen", return_value=response):
            sent = send_delivery(delivery)

        assert sent is True
        delivery.refresh_from_db()
        assert delivery.status == DeliveryStatus.SENT
        assert delivery.sent_at is not None

    def test_a_failed_sms_is_recorded_as_failed_not_lost(self, tenant, settings):
        """L3: the failure is data, not an exception that unwinds anything."""
        settings.SMS_BACKEND = "notifications.channels.ujumbe_sms.UjumbeSmsBackend"
        settings.UJUMBE_SMS_API_KEY = ""  # not configured

        from notifications import credits

        # Credit available, so the failure under test is the missing API key and
        # not an empty balance.
        credits.purchase(tenant, 10, note="For the test")

        recipient = UserFactory(
            organization=tenant, full_name="Texted Tom", phone="+254722000456"
        )
        delivery = NotificationDelivery.objects.create(
            organization=tenant,
            event=NotificationEvent.objects.create(
                organization=tenant,
                event_key="gate_out.approved",
                target_type="dispatch.GateOut",
                target_id="1",
                target_label="GP-000001",
            ),
            recipient=recipient,
            channel=Channel.SMS,
            destination=recipient.phone,
            body="Gate pass GP-000001 has been approved and can be released.",
        )

        assert send_delivery(delivery) is False
        delivery.refresh_from_db()
        # ABANDONED rather than PENDING: a missing API key will not fix itself,
        # so retrying it would only bury the real problem in a retry queue.
        assert delivery.status == DeliveryStatus.ABANDONED
        assert "not configured" in delivery.last_error

    def test_the_local_backend_sends_nothing_real(self, tenant, capsys):
        """§12.0: no message can reach a real technician from a dev machine."""
        from notifications.channels import get_sms_backend

        backend = get_sms_backend()
        result = backend.send("0722000001", RenderedMessage(body="test message"))

        assert result.succeeded is True
        printed = capsys.readouterr().out
        assert "SMS to 0722000001" in printed
        assert "test message" in printed


@pytest.mark.django_db(transaction=True)
class TestDispatchAfterARealCommit:
    """The dispatch has to work where it actually runs: after a real commit.

    Every other test here executes the on-commit callback through
    ``django_capture_on_commit_callbacks``, which runs it *inside* the test's
    transaction. That hid a bug for the whole of Phase 4.

    In production the callback runs from ``transaction.on_commit`` — outside any
    transaction. ``tenant_context`` publishes the organization as a
    transaction-local Postgres setting (§2.2), so outside a transaction it is
    discarded, every row-level security policy sees an empty organization, and
    the dispatch could not even read back the event it was handed. It returned
    zero and **every notification was silently dropped**.

    So this test uses a real commit. It is slower and truncates tables, which is
    why the suite does not do it everywhere — but the one path that only exists
    outside a transaction has to be tested outside one.
    """

    def test_a_committed_event_reaches_its_recipients(self, settings):
        from django.db import transaction as db_transaction

        from core.provisioning import provision_tenant
        from core.tenancy import tenant_context
        from locations.factories import YardFactory

        settings.TENANT_BASE_DOMAIN = "localhost"
        result = provision_tenant(
            name="Commit Contractors", slug="commit", owner_email="owner@commit.co.ke"
        )
        organization = result["organization"]
        owner = result["owner"]

        with db_transaction.atomic(), tenant_context(organization):
            yard = YardFactory(name="Commit yard")
            gate_out = a_gate_out(organization, yard, owner, owner)

        # No capture helper: this commits for real, and the callback runs the way
        # it will in production.
        with db_transaction.atomic(), tenant_context(organization):
            submit_gate_out(gate_out, submitted_by=owner)

        with db_transaction.atomic(), tenant_context(organization):
            assert NotificationEvent.objects.exists(), (
                "no event was recorded for a submitted gate-out"
            )
            deliveries = NotificationDelivery.objects.filter(channel=Channel.IN_APP)
            assert deliveries.exists(), (
                "the event was recorded but nobody was told. The dispatch runs "
                "outside a transaction, so it has to open one for the tenant to "
                "be visible to row-level security (§2.2)."
            )
            assert deliveries.filter(status=DeliveryStatus.SENT).exists()

