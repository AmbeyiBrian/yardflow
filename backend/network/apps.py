from django.apps import AppConfig


class NetworkConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "network"

    def ready(self) -> None:
        from network import signals  # noqa: F401

        # T1.20: declares how the A3 isolation suite exercises this app's
        # endpoints. Without it the suite fails rather than skipping them.
        from network.isolation import register as register_isolation_fixtures

        register_isolation_fixtures()

