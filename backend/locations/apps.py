from django.apps import AppConfig


class LocationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "locations"

    def ready(self) -> None:
        # T1.18: a new tenant arrives with one yard, its quarantine location and
        # the system stock nodes. Ordered before the catalogue so the yard exists
        # first.
        from core.provisioning import register_tenant_seeder
        from locations import signals  # noqa: F401
        from locations.nodes import seed_locations_and_nodes

        register_tenant_seeder(
            seed_locations_and_nodes, order=10, name="yard, quarantine and stock nodes"
        )

        # T1.20: declares how the A3 isolation suite exercises this app's
        # endpoints. Without it the suite fails rather than skipping them.
        from locations.isolation import register as register_isolation_fixtures

        register_isolation_fixtures()
