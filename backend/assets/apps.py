from django.apps import AppConfig


class AssetsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "assets"

    def ready(self) -> None:
        from assets.isolation import register as register_isolation_fixtures

        register_isolation_fixtures()
