from django.apps import AppConfig


class CatalogueConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "catalogue"

    def ready(self) -> None:
        # T2.4: every new tenant starts with a usable catalogue (C3, D10).
        # Registered as a provisioning seeder rather than called directly from
        # `provision_tenant`, so core does not have to import this app.
        from catalogue.seed import seed_starter_catalogue
        from core.provisioning import register_tenant_seeder

        register_tenant_seeder(
            seed_starter_catalogue, order=30, name="starter catalogue"
        )

        # T1.20: declares how the A3 isolation suite exercises this app's
        # endpoints. Without it the suite fails rather than skipping them.
        from catalogue.isolation import register as register_isolation_fixtures

        register_isolation_fixtures()
