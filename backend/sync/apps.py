from django.apps import AppConfig


class SyncConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sync'

    def ready(self) -> None:
        # T1.20: how the A3 suite exercises the sync endpoints.
        from sync.isolation import register as register_isolation_fixtures

        register_isolation_fixtures()
