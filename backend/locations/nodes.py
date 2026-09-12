"""Creating and finding stock nodes (design §3.1, §4.5; C4, J1).

Nodes are created automatically, never by hand. §3.1: "nodes are auto-created and
never deleted." If creating a node were a separate step someone had to remember,
the first forgotten one would be a movement that cannot be posted — during a
delivery, at the gate, with a driver waiting.
"""

from __future__ import annotations

from locations.models import Location, LocationType, NodeType, StockNode


def node_for_location(location: Location) -> StockNode:
    node, _ = StockNode.objects.get_or_create(
        organization_id=location.organization_id,
        type=NodeType.LOCATION,
        location=location,
        defaults={"label": location.name},
    )
    return node


def node_for_user(user) -> StockNode:
    """The custody node for a person (I1).

    Created on first custody rather than with the user, per §3.1 — most users
    never hold stock, and a node per user would be noise in every picker.
    """
    node, _ = StockNode.objects.get_or_create(
        organization_id=user.organization_id,
        type=NodeType.PERSON,
        user=user,
        defaults={"label": str(user)},
    )
    return node


def node_for_site(site) -> StockNode:
    """Where installed material lives (H2)."""
    node, _ = StockNode.objects.get_or_create(
        organization_id=site.organization_id,
        type=NodeType.SITE,
        site=site,
        defaults={"label": str(site)},
    )
    return node


def node_for_client(client) -> StockNode:
    """Where material returned to a client sits, in transit (K1, K3)."""
    node, _ = StockNode.objects.get_or_create(
        organization_id=client.organization_id,
        type=NodeType.CLIENT,
        client=client,
        defaults={"label": f"{client.name} (returned)"},
    )
    return node


def external_node(organization_id, client=None) -> StockNode:
    """Where receipts come from (§3.1).

    A named client's issuing store when consignment stock arrives, otherwise the
    generic supplier node. Receipts need a source node because every movement is
    double-entry: material appearing from nowhere would break the invariant that
    inbound minus outbound equals the balance (§3.2).
    """
    if client is not None:
        node, _ = StockNode.objects.get_or_create(
            organization_id=organization_id,
            type=NodeType.EXTERNAL,
            client=client,
            defaults={"label": f"{client.name} (issuing store)"},
        )
        return node

    node, _ = StockNode.objects.get_or_create(
        organization_id=organization_id,
        type=NodeType.EXTERNAL,
        client=None,
        defaults={"label": "Suppliers and external sources"},
    )
    return node


def consumed_node(organization_id) -> StockNode:
    """Where bulk material used up on site goes (H2)."""
    node, _ = StockNode.objects.get_or_create(
        organization_id=organization_id,
        type=NodeType.CONSUMED,
        defaults={"label": "Consumed on site"},
    )
    return node


def scrap_node(organization_id) -> StockNode:
    """Where disposed material goes (J3)."""
    node, _ = StockNode.objects.get_or_create(
        organization_id=organization_id,
        type=NodeType.SCRAP,
        defaults={"label": "Scrapped"},
    )
    return node


def quarantine_location(organization_id, yard: Location | None = None) -> Location:
    """The quarantine location for a yard (J1).

    One per yard, created with the yard. Faulty, damaged and scrap lines land
    here on receipt rather than in free stock (D2), so it has to exist before the
    first gate-in — not be created on demand halfway through one.
    """
    if yard is None:
        yard = Location.objects.filter(
            organization_id=organization_id, type=LocationType.YARD, is_active=True
        ).order_by("pk").first()
    if yard is None:
        raise ValueError("This organization has no yard, so it has no quarantine location.")

    existing = Location.objects.filter(
        organization_id=organization_id, parent=yard, type=LocationType.QUARANTINE
    ).first()
    if existing is not None:
        return existing

    return Location.objects.create(
        organization_id=organization_id,
        parent=yard,
        name=f"Quarantine — {yard.name}",
        type=LocationType.QUARANTINE,
        is_system=True,
    )


def ensure_yard_quarantine(yard: Location) -> Location | None:
    """Give a yard its quarantine child (C4, J1)."""
    if yard.type != LocationType.YARD:
        return None
    return quarantine_location(yard.organization_id, yard)


def seed_locations_and_nodes(organization) -> dict:
    """Provisioning seeder: one yard, its quarantine, and the system nodes (A1).

    T1.18 requires a new tenant to arrive with "one yard with its quarantine
    location, the system stock nodes". Registered as a seeder so provisioning
    does not need to import this app.
    """
    yard = Location.objects.filter(
        organization=organization, type=LocationType.YARD
    ).first()
    if yard is None:
        yard = Location.objects.create(
            organization=organization, name="Main yard", type=LocationType.YARD
        )

    quarantine = ensure_yard_quarantine(yard)

    return {
        "yard": yard.name,
        "quarantine": quarantine.name if quarantine else None,
        "consumed_node": consumed_node(organization.pk).label,
        "scrap_node": scrap_node(organization.pk).label,
        "external_node": external_node(organization.pk).label,
    }
