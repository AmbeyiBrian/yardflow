"""Isolation fixtures for the dispatch endpoints (T1.20, A3)."""

from decimal import Decimal

from core.isolation import register_isolation_fixture


def register() -> None:
    from accounts.models import User
    from catalogue.models import ItemType
    from dispatch.models import GateOut, GateOutLine, GateOutPurpose, ReleaseVariance
    from locations.models import Location, LocationType
    from network.models import Client, Site

    def _prerequisites(organization):
        yard = Location.objects.filter(
            organization=organization, type=LocationType.YARD
        ).first() or Location.objects.create(
            organization=organization, name="Isolation yard", type=LocationType.YARD
        )
        holder = User.objects.filter(organization=organization).first() or (
            User.objects.create_user(
                email="iso-holder@example.com", organization=organization
            )
        )
        client = Client.objects.filter(organization=organization).first() or (
            Client.objects.create(organization=organization, name="Isolation client")
        )
        site = Site.objects.filter(organization=organization).first() or Site.objects.create(
            organization=organization,
            client=client,
            internal_ref="ISO-GO-1",
            name="Isolation site",
        )
        return yard, holder, site

    def make_gate_out(organization):
        yard, holder, site = _prerequisites(organization)
        return GateOut.objects.create(
            organization=organization,
            from_location=yard,
            site=site,
            custody_holder=holder,
            requested_by=holder,
            purpose_type=GateOutPurpose.INSTALLATION,
        )

    def make_variance(organization):
        from catalogue.models import ItemCategory

        gate_out = make_gate_out(organization)
        category = ItemCategory.objects.filter(organization=organization).first() or (
            ItemCategory.objects.create(organization=organization, name="Isolation")
        )
        item = ItemType.objects.filter(organization=organization).first() or (
            ItemType.objects.create(
                organization=organization, category=category, name="Isolation item"
            )
        )
        line = GateOutLine.objects.create(
            organization=organization,
            gate_out=gate_out,
            item_type=item,
            tracking_mode=item.default_tracking_mode,
            requested_qty=Decimal("2"),
            uom=item.uom,
        )
        return ReleaseVariance.objects.create(
            organization=organization,
            gate_out_line=line,
            approved_qty=Decimal("2"),
            released_qty=Decimal("1"),
            reason="Isolation fixture",
        )

    register_isolation_fixture("gate-out", make_gate_out, payload={"notes": "renamed"})
    register_isolation_fixture("release-variance", make_variance)
