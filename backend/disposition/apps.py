from django.apps import AppConfig


class DispositionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'disposition'

    def ready(self) -> None:
        # T1.20: how the A3 suite exercises the Phase 6 endpoints.
        from disposition.isolation import register as register_isolation_fixtures

        register_isolation_fixtures()
