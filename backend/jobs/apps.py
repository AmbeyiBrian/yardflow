from django.apps import AppConfig


class JobsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'jobs'

    def ready(self) -> None:
        # T1.20: declares how the A3 isolation suite exercises this app's
        # endpoints. Without it the suite fails rather than skipping them.
        from jobs.isolation import register as register_isolation_fixtures

        register_isolation_fixtures()
