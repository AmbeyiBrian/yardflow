"""Isolation fixtures for the Phase 6 endpoints (T1.20, A3).

These three carry some of the most sensitive rows in the product: a disposal is
evidence of a write-off, and a return acknowledgement is evidence that liability
ended. A cross-tenant read here would hand a competitor the record of what
somebody else destroyed and what they are no longer answerable for.
"""

from decimal import Decimal

from core.isolation import register_isolation_fixture


def register() -> None:
    from catalogue.models import ItemCategory, ItemType
    from disposition.models import (
        ClientReturnAck,
        Disposal,
        DisposalLine,
        DisposalMethod,
        Disposition,
        DispositionDecision,
        DispositionLine,
    )
    from locations.models import Location, LocationType
    from locations.nodes import quarantine_location

    def _item(organization) -> ItemType:
        category = ItemCategory.objects.filter(organization=organization).first() or (
            ItemCategory.objects.create(organization=organization, name="Isolation")
        )
        return ItemType.objects.filter(organization=organization).first() or (
            ItemType.objects.create(
                organization=organization, category=category, name="Isolation item"
            )
        )

    def _quarantine(organization) -> Location:
        yard = Location.objects.filter(
            organization=organization, type=LocationType.YARD
        ).first() or Location.objects.create(
            organization=organization, name="Isolation yard", type=LocationType.YARD
        )
        return quarantine_location(organization.pk, yard)

    def make_disposition(organization):
        item = _item(organization)
        disposition = Disposition.objects.create(
            organization=organization,
            from_location=_quarantine(organization),
            decision=DispositionDecision.SCRAP,
            reason="Isolation fixture.",
        )
        DispositionLine.objects.create(
            organization=organization,
            disposition=disposition,
            item_type=item,
            quantity=Decimal("1"),
            uom=item.uom,
        )
        return disposition

    def make_disposal(organization):
        item = _item(organization)
        disposal = Disposal.objects.create(
            organization=organization,
            from_location=_quarantine(organization),
            method=DisposalMethod.OTHER,
        )
        DisposalLine.objects.create(
            organization=organization,
            disposal=disposal,
            item_type=item,
            quantity=Decimal("1"),
            uom=item.uom,
        )
        return disposal

    def make_ack(organization):
        from django.utils import timezone

        from accounts.models import User
        from dispatch.models import GateOut, GateOutPurpose
        from network.models import Client

        client = Client.objects.filter(organization=organization).first() or (
            Client.objects.create(organization=organization, name="Isolation client")
        )
        yard = Location.objects.filter(
            organization=organization, type=LocationType.YARD
        ).first() or Location.objects.create(
            organization=organization, name="Isolation yard", type=LocationType.YARD
        )
        user = User.objects.filter(organization=organization).first()

        gate_out = GateOut.objects.create(
            organization=organization,
            from_location=yard,
            client=client,
            custody_holder=user,
            requested_by=user,
            purpose_type=GateOutPurpose.RETURN_TO_CLIENT,
        )
        # Created directly rather than through `acknowledge_client_return`: the
        # service rightly refuses an unreleased pass, and the fixture only needs
        # a row to try reading across tenants.
        return ClientReturnAck.objects.create(
            organization=organization,
            gate_out=gate_out,
            acknowledged_ref="ISO-ACK-1",
            acknowledged_at=timezone.now(),
        )

    register_isolation_fixture(
        "disposition", make_disposition, payload={"notes": "renamed"}
    )
    register_isolation_fixture("disposal", make_disposal, payload={"notes": "renamed"})
    # No payload: an acknowledgement is a record of something that happened, so
    # the endpoint accepts no PATCH (see the viewset).
    register_isolation_fixture("client-return-ack", make_ack)
