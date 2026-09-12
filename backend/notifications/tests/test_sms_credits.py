"""SMS runs on credits the tenant buys (L4; §9.1a).

One SMS is one credit, one credit is KES 1. Two properties matter more than the
arithmetic:

* **A failed send costs nothing.** The credit is taken before the provider is
  called — two dispatches must not both spend the last one — and given back if
  the message is refused.
* **At zero it stops and says so**, without retrying. A message there is no
  credit for will not succeed on the fourth attempt, and retrying it hides the
  reason messages stopped arriving.
"""

from __future__ import annotations

from unittest import mock

import pytest

from notifications import credits
from notifications.channels.base import DeliveryResult
from notifications.matrix import Channel
from notifications.models import DeliveryStatus, NotificationDelivery, SmsCreditEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def delivery(tenant, django_user_model):
    """One pending SMS, ready to send."""
    from notifications.models import NotificationEvent

    person = django_user_model.objects.create_user(
        email="tech@silvertech.co.ke",
        password="a good long password",
        organization=tenant,
        phone="0722000001",
    )
    event = NotificationEvent.objects.create(
        organization=tenant, event_key="custody.overdue", payload={}
    )
    return NotificationDelivery.objects.create(
        organization=tenant,
        event=event,
        recipient=person,
        channel=Channel.SMS,
        subject="Overdue",
        body="Please bring it back.",
        destination="0722000001",
        status=DeliveryStatus.PENDING,
    )


def sends(succeeded: bool):
    """A stand-in provider with a fixed answer."""
    channel = mock.MagicMock()
    channel.send.return_value = DeliveryResult(
        succeeded=succeeded, error="" if succeeded else "Rejected", retryable=False
    )
    return channel


class TestTheLedger:
    def test_the_balance_is_the_sum_of_the_entries(self, tenant):
        credits.purchase(tenant, 500, note="Paid by M-Pesa")
        credits.purchase(tenant, 100, note="Top-up")

        assert credits.balance(tenant) == 600
        # The cached figure and the ledger must agree — the ledger settles it.
        assert credits.ledger_balance(tenant) == 600

    def test_spending_writes_an_entry_rather_than_editing_a_number(self, tenant, delivery):
        credits.purchase(tenant, 10)

        credits.spend_one(tenant, delivery)

        assert credits.balance(tenant) == 9
        entry = SmsCreditEntry.objects.get(kind=SmsCreditEntry.Kind.CONSUMPTION)
        assert entry.quantity == -1
        assert entry.delivery_id == delivery.pk

    def test_spending_what_is_not_there_is_refused(self, tenant, delivery):
        with pytest.raises(credits.NoCredit):
            credits.spend_one(tenant, delivery)

        assert credits.balance(tenant) == 0

    def test_a_refund_reverses_rather_than_deletes(self, tenant, delivery):
        """The attempt happened. The record of it is worth more than a tidy
        ledger."""
        credits.purchase(tenant, 5)
        spent = credits.spend_one(tenant, delivery)

        credits.refund(spent, note="Provider did not accept the message.")

        assert credits.balance(tenant) == 5
        assert SmsCreditEntry.objects.count() == 3, "purchase, spend, reversal"

    def test_an_adjustment_needs_a_reason(self, tenant):
        with pytest.raises(ValueError):
            credits.adjust(tenant, 10, note="")


class TestSending:
    def test_a_sent_message_costs_one_credit(self, tenant, delivery):
        from notifications.events import send_delivery

        credits.purchase(tenant, 10)

        with mock.patch("notifications.channels.get_channel", return_value=sends(True)):
            assert send_delivery(delivery) is True

        assert credits.balance(tenant) == 9
        delivery.refresh_from_db()
        assert delivery.status == DeliveryStatus.SENT

    def test_a_refused_message_costs_nothing(self, tenant, delivery):
        """Charged on acceptance, as an owner would expect — but taken first, so
        two dispatches cannot both spend the last credit."""
        from notifications.events import send_delivery

        credits.purchase(tenant, 10)

        with mock.patch("notifications.channels.get_channel", return_value=sends(False)):
            send_delivery(delivery)

        assert credits.balance(tenant) == 10

    def test_with_no_credit_the_provider_is_never_called(self, tenant, delivery):
        from notifications.events import send_delivery

        channel = sends(True)
        with mock.patch("notifications.channels.get_channel", return_value=channel):
            assert send_delivery(delivery) is False

        assert channel.send.call_count == 0, "no credit, no request, no cost"

    def test_running_out_is_recorded_where_an_admin_will_read_it(self, tenant, delivery):
        """The failure this prevents is silent: messages that stop arriving."""
        from notifications.events import send_delivery

        with mock.patch("notifications.channels.get_channel", return_value=sends(True)):
            send_delivery(delivery)

        delivery.refresh_from_db()
        assert delivery.status == DeliveryStatus.FAILED
        assert "No SMS credit" in delivery.last_error
        assert delivery.status != DeliveryStatus.PENDING, (
            "not queued for retry — the fourth attempt will not succeed either"
        )

    def test_other_channels_are_not_metered(self, tenant, delivery):
        """Only SMS costs money per message. Email and in-app must keep working
        when the credit runs out."""
        from notifications.events import send_delivery

        delivery.channel = Channel.IN_APP
        delivery.save(update_fields=["channel"])

        assert send_delivery(delivery) is True
        delivery.refresh_from_db()
        assert delivery.status == DeliveryStatus.SENT


class TestWarningBeforeItStops:
    def test_a_low_balance_is_flagged(self, tenant):
        credits.purchase(tenant, 10)

        assert credits.is_low(tenant) is True

    def test_a_healthy_balance_is_not(self, tenant):
        credits.purchase(tenant, 500)

        assert credits.is_low(tenant) is False
