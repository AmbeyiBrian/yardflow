"""T2.5, T2.6 — locations and the stock node graph (§3.1, §4.5; C4, J1)."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from accounts.factories import UserFactory
from locations.factories import StoreFactory, VehicleFactory, YardFactory
from locations.models import (
    UNAVAILABLE_NODE_TYPES,
    Location,
    LocationType,
    NodeType,
    StockNode,
)
from locations.nodes import (
    consumed_node,
    external_node,
    node_for_client,
    node_for_user,
    quarantine_location,
    scrap_node,
    seed_locations_and_nodes,
)
from network.factories import ClientFactory, SiteFactory


class TestLocationTree:
    """C4: locations form a tree, yard -> store."""

    def test_a_store_sits_inside_a_yard(self, tenant):
        yard = YardFactory(name="Main yard")
        store = StoreFactory(name="Bonded store", parent=yard)

        assert store.parent == yard
        assert store.yard == yard

    def test_a_yard_cannot_have_a_parent(self, tenant):
        yard = YardFactory()

        with pytest.raises(ValidationError, match="a_yard_has_no_parent"):
            Location.objects.create(
                organization=yard.organization,
                name="Nested yard",
                type=LocationType.YARD,
                parent=yard,
            )

    def test_a_store_must_have_a_parent(self, tenant):
        with pytest.raises(ValidationError, match="a_store_sits_inside_a_yard"):
            Location.objects.create(
                organization=tenant, name="Orphan store", type=LocationType.STORE
            )

    def test_the_tree_rules_are_enforced_by_the_database_too(self, tenant):
        """Defence in depth: `save()` validates, and so does Postgres.

        The Python check gives a readable message; the constraint is what holds
        against a raw query, a data migration or a psql session.
        """
        from django.db import connection

        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO locations_location "
                    "(organization_id, name, code, type, vehicle_reg, is_active, "
                    " is_system, created_at, updated_at) "
                    "VALUES (%s, %s, '', %s, '', true, false, now(), now())",
                    [str(tenant.pk), "Orphan store", LocationType.STORE],
                )

    def test_a_location_cannot_contain_itself(self, tenant):
        yard = YardFactory()
        yard.parent = yard

        with pytest.raises(ValidationError):
            yard.save()

    def test_a_vehicle_is_a_location_so_transit_stays_visible(self, tenant):
        """C4: "vehicles are locations, so material in transit remains visible"."""
        vehicle = VehicleFactory(name="Pickup 1", vehicle_reg="KDA 123X")

        assert vehicle.type == LocationType.VEHICLE
        assert vehicle.node.type == NodeType.LOCATION

    def test_a_vehicle_needs_a_registration(self, tenant):
        """G2: a load must be attributable to a vehicle."""
        with pytest.raises(ValidationError, match="registration"):
            VehicleFactory(vehicle_reg="")


class TestQuarantineIsAutomatic:
    """C4, J1: every yard has a quarantine child."""

    def test_creating_a_yard_creates_its_quarantine(self, tenant):
        """It must exist before the first gate-in.

        D2 routes faulty, damaged and scrap lines to quarantine on receipt. A
        gate-in cannot stop halfway through to create somewhere to put them.
        """
        yard = YardFactory(name="Main yard")

        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)
        assert quarantine.is_system is True

    def test_the_quarantine_helper_is_idempotent(self, tenant):
        yard = YardFactory()

        first = quarantine_location(tenant.pk, yard)
        second = quarantine_location(tenant.pk, yard)

        assert first == second

    def test_quarantine_never_counts_as_available_stock(self, tenant):
        """J1: "quarantined stock never appears as available"."""
        yard = YardFactory()
        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)

        assert quarantine.node.holds_available_stock is False
        assert yard.node.holds_available_stock is True

    def test_the_quarantine_location_cannot_be_deleted(self, tenant):
        yard = YardFactory()
        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)

        with pytest.raises(ValidationError, match="cannot"):
            quarantine.delete()

    def test_a_location_with_children_cannot_be_deleted(self, tenant):
        """C4: deactivate, do not delete."""
        yard = YardFactory()

        with pytest.raises(ValidationError, match="Deactivate"):
            yard.delete()


class TestStockNodeArity:
    """§3.1: the database enforces that a node points at exactly one thing."""

    def test_a_location_gets_a_node_automatically(self, tenant):
        yard = YardFactory(name="Main yard")

        assert yard.node.type == NodeType.LOCATION
        assert yard.node.label == "Main yard"

    def test_a_node_cannot_point_at_two_things(self, tenant):
        """Ambiguity here would make "where is this?" unanswerable.

        The ledger has no way to recover from a node that is both a location and
        a person, so the constraint lives in the database.
        """
        yard = YardFactory()
        user = UserFactory(organization=tenant)

        with pytest.raises(IntegrityError), transaction.atomic():
            StockNode.objects.create(
                organization=tenant,
                type=NodeType.LOCATION,
                location=yard,
                user=user,
                label="Impossible",
            )

    def test_a_typed_node_without_its_target_is_refused(self, tenant):
        with pytest.raises(IntegrityError), transaction.atomic():
            StockNode.objects.create(
                organization=tenant, type=NodeType.SITE, label="No site"
            )

    def test_a_system_node_carries_no_target(self, tenant):
        assert consumed_node(tenant.pk).type == NodeType.CONSUMED
        assert scrap_node(tenant.pk).location_id is None

    def test_only_one_consumed_node_per_tenant(self, tenant):
        first = consumed_node(tenant.pk)
        second = consumed_node(tenant.pk)

        assert first == second
        with pytest.raises(IntegrityError), transaction.atomic():
            StockNode.objects.create(
                organization=tenant, type=NodeType.CONSUMED, label="Another"
            )


class TestNodesForPeopleSitesAndClients:
    def test_a_person_node_is_created_on_first_custody(self, tenant):
        """I1: custody is material held by a person rather than a location.

        Created on demand, not with the user: most users never hold stock, and a
        node each would be noise in every picker.
        """
        user = UserFactory(organization=tenant)
        assert not StockNode.objects.filter(user=user).exists()

        node = node_for_user(user)

        assert node.type == NodeType.PERSON
        assert node_for_user(user) == node

    def test_a_site_gets_a_node_when_it_is_created(self, tenant):
        """H2: installed material is recorded against the site."""
        site = SiteFactory()

        assert site.node.type == NodeType.SITE

    def test_a_client_gets_both_a_return_node_and_an_issuing_node(self, tenant):
        """K1/K3 versus consignment receipt — two different places.

        Material returned *to* a client sits in transit and stays our exposure
        until acknowledged; material issued *by* them is where consignment stock
        comes from. Conflating them would make the client position report wrong.
        """
        client = ClientFactory(name="Safaricom")

        assert node_for_client(client).type == NodeType.CLIENT
        assert external_node(tenant.pk, client=client).type == NodeType.EXTERNAL
        assert node_for_client(client) != external_node(tenant.pk, client=client)

    def test_the_generic_external_node_is_shared(self, tenant):
        assert external_node(tenant.pk) == external_node(tenant.pk)


class TestAvailability:
    """§3.3: which nodes count as available stock."""

    def test_the_unavailable_types_are_the_documented_ones(self):
        assert set(UNAVAILABLE_NODE_TYPES) == {
            NodeType.SITE,
            NodeType.CLIENT,
            NodeType.CONSUMED,
            NodeType.SCRAP,
            NodeType.EXTERNAL,
        }

    def test_available_excludes_quarantine_site_client_consumed_and_scrap(self, tenant):
        yard = YardFactory()
        SiteFactory()
        ClientFactory()
        consumed_node(tenant.pk)
        scrap_node(tenant.pk)

        available = set(StockNode.objects.available().values_list("type", flat=True))

        assert NodeType.LOCATION in available
        assert not available & set(UNAVAILABLE_NODE_TYPES)
        # The quarantine location is a LOCATION node, so it has to be excluded by
        # its location type rather than by node type.
        quarantine = Location.objects.get(parent=yard, type=LocationType.QUARANTINE)
        assert quarantine.node not in set(StockNode.objects.available())

    def test_a_vehicle_still_counts_as_available(self, tenant):
        """Material on a vehicle has left the store but is still ours."""
        vehicle = VehicleFactory()

        assert vehicle.node in set(StockNode.objects.available())


class TestProvisioningSeed:
    """T1.18: a new tenant arrives with a yard, quarantine and system nodes."""

    def test_seeding_creates_the_yard_and_its_quarantine(self, tenant):
        result = seed_locations_and_nodes(tenant)

        assert result["yard"] == "Main yard"
        assert "Quarantine" in result["quarantine"]

    def test_seeding_creates_the_system_nodes(self, tenant):
        seed_locations_and_nodes(tenant)

        types = set(StockNode.objects.values_list("type", flat=True))
        assert {NodeType.CONSUMED, NodeType.SCRAP, NodeType.EXTERNAL} <= types

    def test_a_provisioned_tenant_can_receive_stock_immediately(self, db):
        """The point of the seed: no setup step between onboarding and use."""
        from core.provisioning import provision_tenant
        from core.tenancy import tenant_context

        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )

        with tenant_context(result["organization"]):
            assert Location.objects.filter(type=LocationType.YARD).exists()
            assert Location.objects.filter(type=LocationType.QUARANTINE).exists()
            assert StockNode.objects.filter(type=NodeType.CONSUMED).exists()
            assert StockNode.objects.filter(type=NodeType.EXTERNAL).exists()


class TestLocationIsolation:
    def test_locations_are_scoped_to_their_organization(
        self, organization, other_organization
    ):
        from core.tenancy import tenant_context

        with tenant_context(organization):
            YardFactory(name="Ours")
        with tenant_context(other_organization):
            YardFactory(name="Theirs")

        with tenant_context(organization):
            names = set(Location.objects.values_list("name", flat=True))
            assert "Theirs" not in names
