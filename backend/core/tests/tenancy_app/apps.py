from django.apps import AppConfig


class TenancyAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core.tests.tenancy_app"
    label = "tenancy_app"
