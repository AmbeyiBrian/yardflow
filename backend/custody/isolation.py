"""Isolation fixtures for the custody endpoints (T1.20, A3)."""

from decimal import Decimal

from core.isolation import register_isolation_fixture


def register() -> None:
    from accounts.models import User
    from catalogue.models import ItemCategory, ItemType
    from custody.models import CustodyExpectation, CustodyTransfer, CustodyTransferLine

    def _people(organization):
        """Two users, because a handover needs two different people (I5)."""
        holder = User.objects.filter(organization=organization).first() or (
            User.objects.create_user(
                email="iso-holder-a@example.com", organization=organization
            )
        )
        receiver = (
            User.objects.filter(organization=organization).exclude(pk=holder.pk).first()
            or User.objects.create_user(
                email="iso-holder-b@example.com", organization=organization
            )
        )
        return holder, receiver

    def _item(organization):
        category = ItemCategory.objects.filter(organization=organization).first() or (
            ItemCategory.objects.create(organization=organization, name="Isolation")
        )
        return ItemType.objects.filter(organization=organization).first() or (
            ItemType.objects.create(
                organization=organization, category=category, name="Isolation item"
            )
        )

    def make_expectation(organization):
        holder, _receiver = _people(organization)
        return CustodyExpectation.objects.create(
            organization=organization,
            holder=holder,
            item_type=_item(organization),
            quantity=Decimal("1"),
        )

    def make_transfer(organization):
        holder, receiver = _people(organization)
        transfer = CustodyTransfer.objects.create(
            organization=organization, from_holder=holder, to_holder=receiver
        )
        item = _item(organization)
        CustodyTransferLine.objects.create(
            organization=organization,
            transfer=transfer,
            item_type=item,
            quantity=Decimal("1"),
            uom=item.uom,
        )
        return transfer

    register_isolation_fixture("custody-expectation", make_expectation)
    register_isolation_fixture(
        "custody-transfer", make_transfer, payload={"notes": "renamed"}
    )
