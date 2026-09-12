"""Locations and the stock node graph (design §3.1, §4.5; C4, J1).

``StockNode`` is the quiet centre of the whole design. §3.1: "a node is anywhere
material can be." Because custody, installation, quarantine, consumption, client
returns and transfers are all *movements between nodes*, they collapse into one
query — and H4's reconciliation (issued vs installed vs returned vs unaccounted)
becomes a single aggregation grouped by destination node type, rather than five
bespoke reports that can disagree with each other.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel
from core.tenancy import TenantManager, TenantModel, TenantQuerySet


class LocationType(models.TextChoices):
    YARD = "YARD", "Yard"
    STORE = "STORE", "Store within a yard"
    # C4: "vehicles are locations, so material in transit remains visible."
    VEHICLE = "VEHICLE", "Vehicle"
    # J1: a system location. Quarantined stock never appears as available.
    QUARANTINE = "QUARANTINE", "Quarantine"


class Location(TenantModel, TimeStampedModel):
    """A physical stock-holding place (C4).

    A tree: yard -> store. Vehicles sit at the top level because a vehicle is not
    inside a yard — it is where material goes when it leaves one, and keeping it
    visible is the point (C4).
    """

    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="children"
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=50, blank=True)
    type = models.CharField(max_length=20, choices=LocationType.choices)

    # G2: recorded on release so a load is attributable.
    vehicle_reg = models.CharField(max_length=30, blank=True)

    # C4: a location holding stock cannot be deleted, only deactivated.
    is_active = models.BooleanField(default=True, db_index=True)

    # Set on the quarantine location created automatically for each yard, so it
    # can be found again without matching on its name (J1).
    is_system = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "parent", "name"],
                name="uniq_location_name_per_parent",
            ),
            # A store must sit inside something; a yard must not.
            models.CheckConstraint(
                condition=~Q(type=LocationType.YARD) | Q(parent__isnull=True),
                name="a_yard_has_no_parent",
            ),
            models.CheckConstraint(
                condition=~Q(type=LocationType.STORE) | Q(parent__isnull=False),
                name="a_store_sits_inside_a_yard",
            ),
            # A vehicle with no registration cannot be identified at the gate.
            models.CheckConstraint(
                condition=~Q(type=LocationType.VEHICLE) | ~Q(vehicle_reg=""),
                name="a_vehicle_has_a_registration",
            ),
        ]
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    @property
    def is_quarantine(self) -> bool:
        return self.type == LocationType.QUARANTINE

    @property
    def yard(self) -> Location | None:
        """The yard this location belongs to, if any."""
        if self.type == LocationType.YARD:
            return self
        parent = self.parent
        if parent is not None:
            return parent.yard
        return None

    def clean(self) -> None:
        super().clean()

        if self.parent_id is not None:
            if self.parent_id == self.pk:
                raise ValidationError({"parent": "A location cannot contain itself."})
            ancestor = self.parent
            seen: set[int] = set()
            while ancestor is not None:
                if ancestor.pk == self.pk or ancestor.pk in seen:
                    raise ValidationError({"parent": "That would create a loop."})
                seen.add(ancestor.pk)
                ancestor = ancestor.parent

        if self.type == LocationType.VEHICLE and not self.vehicle_reg:
            raise ValidationError(
                {"vehicle_reg": "A vehicle needs a registration so a load is attributable (G2)."}
            )

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization"])
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        """C4: a location holding stock cannot be deleted, only deactivated.

        The stock check arrives with the ledger (T3.2). Until then this blocks
        deleting a location that has children or a stock node with history,
        which is the part that is knowable now.
        """
        if self.children.exists():
            raise ValidationError(
                "This location contains other locations. Deactivate it instead (C4)."
            )
        if self.is_system:
            raise ValidationError(
                "The quarantine location is part of how the yard works and cannot "
                "be deleted (J1)."
            )
        return super().delete(*args, **kwargs)


class NodeType(models.TextChoices):
    """Where material can be (§3.1).

    Each value is somewhere material genuinely sits, not a status flag. That is
    what lets one ledger answer every question about it.
    """

    LOCATION = "LOCATION", "Yard, store, vehicle or quarantine"
    PERSON = "PERSON", "Held in someone's custody"
    SITE = "SITE", "Installed at a site"
    CLIENT = "CLIENT", "Returned to a client"
    EXTERNAL = "EXTERNAL", "Supplier or client-issuing store"
    CONSUMED = "CONSUMED", "Used up on site"
    SCRAP = "SCRAP", "Disposed of"


#: Node types that never count as available stock (§3.3, J1).
#:
#: SITE and CLIENT are excluded because the material is no longer ours to issue;
#: CONSUMED and SCRAP because it no longer exists. Quarantine is a LOCATION whose
#: location type is QUARANTINE, so it is excluded by that rather than by type.
UNAVAILABLE_NODE_TYPES = (
    NodeType.SITE,
    NodeType.CLIENT,
    NodeType.CONSUMED,
    NodeType.SCRAP,
    NodeType.EXTERNAL,
)


class StockNodeQuerySet(TenantQuerySet):
    def available(self):
        """Nodes whose contents count as available stock (§3.3)."""
        return self.exclude(type__in=UNAVAILABLE_NODE_TYPES).exclude(
            location__type=LocationType.QUARANTINE
        )


class StockNode(TenantModel, TimeStampedModel):  # type: ignore[django-manager-missing]
    """Anywhere material can be (§3.1).

    Nodes are auto-created and never deleted: the ledger references them
    forever, and a node that could disappear would take movement history with
    it (§3.2, M3).
    """

    type = models.CharField(max_length=20, choices=NodeType.choices, db_index=True)

    # Exactly one of these is set, according to `type` — see the check
    # constraint. EXTERNAL is the exception: it may name a client (their issuing
    # store) or nothing at all (the generic supplier node).
    location = models.OneToOneField(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="node"
    )
    user = models.OneToOneField(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="stock_node"
    )
    site = models.OneToOneField(
        "network.Site", on_delete=models.PROTECT, null=True, blank=True, related_name="node"
    )
    client = models.ForeignKey(
        "network.Client", on_delete=models.PROTECT, null=True, blank=True, related_name="nodes"
    )

    label = models.CharField(max_length=200)

    # Built from TenantManager so the organization filter is kept, and
    # `all_objects` stays inherited from TenantModel rather than re-declared.
    objects = TenantManager.from_queryset(StockNodeQuerySet)()  # type: ignore[misc,django-manager-missing]

    class Meta:
        constraints = [
            # §3.1: the arity is enforced by the database, not by convention. A
            # node pointing at two things would make "where is this?" ambiguous,
            # and the ledger has no way to recover from that.
            models.CheckConstraint(
                condition=(
                    (
                        Q(type=NodeType.LOCATION)
                        & Q(location__isnull=False)
                        & Q(user__isnull=True)
                        & Q(site__isnull=True)
                        & Q(client__isnull=True)
                    )
                    | (
                        Q(type=NodeType.PERSON)
                        & Q(user__isnull=False)
                        & Q(location__isnull=True)
                        & Q(site__isnull=True)
                        & Q(client__isnull=True)
                    )
                    | (
                        Q(type=NodeType.SITE)
                        & Q(site__isnull=False)
                        & Q(location__isnull=True)
                        & Q(user__isnull=True)
                        & Q(client__isnull=True)
                    )
                    | (
                        Q(type=NodeType.CLIENT)
                        & Q(client__isnull=False)
                        & Q(location__isnull=True)
                        & Q(user__isnull=True)
                        & Q(site__isnull=True)
                    )
                    | (
                        # EXTERNAL: a named client's issuing store, or the
                        # generic supplier node.
                        Q(type=NodeType.EXTERNAL)
                        & Q(location__isnull=True)
                        & Q(user__isnull=True)
                        & Q(site__isnull=True)
                    )
                    | (
                        # System nodes belong to the tenant, not to any object.
                        Q(type__in=[NodeType.CONSUMED, NodeType.SCRAP])
                        & Q(location__isnull=True)
                        & Q(user__isnull=True)
                        & Q(site__isnull=True)
                        & Q(client__isnull=True)
                    )
                ),
                name="stock_node_arity_matches_its_type",
            ),
            # One CONSUMED and one SCRAP node per tenant (§3.1).
            models.UniqueConstraint(
                fields=["organization", "type"],
                condition=Q(type__in=[NodeType.CONSUMED, NodeType.SCRAP]),
                name="uniq_system_node_per_organization",
            ),
            # One generic EXTERNAL node per tenant; client-specific ones are
            # distinguished by their client.
            models.UniqueConstraint(
                fields=["organization"],
                condition=Q(type=NodeType.EXTERNAL, client__isnull=True),
                name="uniq_generic_external_node_per_organization",
            ),
            models.UniqueConstraint(
                fields=["organization", "client"],
                condition=Q(type=NodeType.EXTERNAL, client__isnull=False),
                name="uniq_client_external_node_per_organization",
            ),
        ]
        indexes = [models.Index(fields=["organization", "type"])]

    def __str__(self) -> str:
        return self.label

    @property
    def holds_available_stock(self) -> bool:
        """Whether material at this node counts as available to issue (§3.3)."""
        if self.type in UNAVAILABLE_NODE_TYPES:
            return False
        location = self.location
        if self.type == NodeType.LOCATION and location is not None and location.is_quarantine:
            # J1: quarantined stock must never be issued by mistake.
            return False
        return True
