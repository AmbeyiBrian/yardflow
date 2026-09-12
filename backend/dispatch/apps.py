from django.apps import AppConfig


class DispatchConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'dispatch'

    def ready(self) -> None:
        # T1.20: declares how the A3 isolation suite exercises this app's
        # endpoints. Without it the suite fails rather than skipping them.
        from dispatch.isolation import register as register_isolation_fixtures

        register_isolation_fixtures()
