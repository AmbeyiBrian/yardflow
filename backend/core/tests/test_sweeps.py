"""The scheduled sweeps (§2.2, §12; I3, F5, K3, Q3, L2, L3).

The first test is the cheap one that matters most. A beat entry naming a task
that does not exist fails **at run time, in production, silently** — the worker
logs an unregistered-task error at 5:30am and nobody is watching. Every other
suite would pass.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from core import sweeps
from locations.factories import YardFactory
from network.factories import ClientFactory
from stock.models import MovementType, OwnerType
from stock.services import MovementRequest, post_movement


class TestTheScheduleIsReal:
    def test_every_beat_entry_names_a_task_that_exists(self):
        """Otherwise the failure is a log line at 5:30am that nobody reads."""
        from celery import current_app

        import core.sweeps  # noqa: F401  — registers the tasks

        registered = set(current_app.tasks.keys())
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            assert entry["task"] in registered, (
                f"Beat entry {name!r} schedules {entry['task']!r}, which is not a "
                f"registered Celery task. It would fail at run time, in "
                f"production, where nobody is watching."
            )

    def test_every_beat_entry_has_a_schedule(self):
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            assert entry.get("schedule") is not None, f"{name} has no schedule."

    def test_the_dispatcher_does_not_require_a_tenant(self):
        """§2.2: it lists organizations and schedules work, and touches no
        tenant data — so it is the one task allowed to run without a tenant."""
        assert sweeps.dispatch_sweeps.requires_organization is False
        assert sweeps.sweep_tenant.requires_organization is True


class TestUnacknowledgedReturnsAreChased:
    """K3, L2: "a beat task notifies on returns still unacknowledged after N days"."""

    @pytest.fixture
    def released_return(self, tenant):
        from dispatch.models import GateOut, GateOutLine, GateOutPurpose
        from dispatch.services import release_gate_out, submit_gate_out
        from locations.nodes import external_node, node_for_location

        yard = YardFactory(name="Sweep yard")
        storekeeper = UserFactory(organization=tenant, full_name="Sara Storekeeper")
        client = ClientFactory(name="Safaricom")
        item = ItemTypeFactory(name="Swept consignment", uom="ea")

        with transaction.atomic():
            post_movement(
                MovementRequest(
                    item_type=item,
                    quantity=Decimal("4"),
                    from_node=external_node(tenant.pk, client=client),
                    to_node=node_for_location(yard),
                    movement_type=MovementType.RECEIPT,
                    owner_type=OwnerType.CLIENT,
                    owner_client=client,
                )
            )

        gate_out = GateOut.objects.create(
            organization=tenant,
            from_location=yard,
            client=client,
            custody_holder=storekeeper,
            requested_by=storekeeper,
            purpose_type=GateOutPurpose.RETURN_TO_CLIENT,
        )
        GateOutLine.objects.create(
            organization=tenant,
            gate_out=gate_out,
            item_type=item,
            tracking_mode="BULK",
            requested_qty=Decimal("4"),
            uom=item.uom,
            owner_type=OwnerType.CLIENT,
            owner_client=client,
        )
        submit_gate_out(gate_out, submitted_by=storekeeper)
        release_gate_out(gate_out, released_by=storekeeper)

        # Long enough ago that the grace period has passed.
        gate_out.released_at = timezone.now() - timedelta(
            days=sweeps.RETURN_ACKNOWLEDGEMENT_GRACE_DAYS + 3
        )
        gate_out.save(update_fields=["released_at"])
        return gate_out

    def test_the_sweep_emits_an_event_for_an_unacknowledged_return(
        self, tenant, released_return
    ):
        from notifications.models import NotificationEvent

        emitted = sweeps._sweep_unacknowledged_returns(tenant.pk)

        assert emitted == 1
        event = NotificationEvent.objects.filter(
            event_key="client_return.unacknowledged"
        ).latest("created_at")
        assert event.target_id == str(released_return.pk)
        # The message has to say how long it has been, or it is a nag rather
        # than information.
        assert event.payload["days_outstanding"] >= (
            sweeps.RETURN_ACKNOWLEDGEMENT_GRACE_DAYS
        )

    def test_an_acknowledged_return_is_not_chased(self, tenant, released_return):
        from disposition.services import acknowledge_client_return

        acknowledge_client_return(released_return, acknowledged_ref="SAF-GRN-1")

        assert sweeps._sweep_unacknowledged_returns(tenant.pk) == 0

    def test_a_recent_return_is_not_chased(self, tenant, released_return):
        """A store with a two-day backlog is not somebody to ring about."""
        released_return.released_at = timezone.now() - timedelta(days=1)
        released_return.save(update_fields=["released_at"])

        assert sweeps._sweep_unacknowledged_returns(tenant.pk) == 0


class TestOneBrokenSweepDoesNotStopTheRest:
    def test_a_failing_step_is_logged_and_the_others_still_run(
        self, tenant, monkeypatch
    ):
        """Expiring a stale gate pass is a control, not a reminder (Q3).

        Losing it because the overdue-custody reminder raised would turn a
        cosmetic failure into a security one.
        """

        def explode(_organization_id):
            raise RuntimeError("the custody sweep is having a bad day")

        monkeypatch.setattr(sweeps, "_sweep_custody_overdue", explode)

        result = sweeps.sweep_tenant(organization_id=str(tenant.pk))

        assert result["custody_overdue"] == "failed"
        # Everything after it still ran.
        assert result["expired_gate_passes"] == 0
        assert result["unacknowledged_returns"] == 0
