from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"

    def ready(self) -> None:
        # L4: a new tenant can send its first invitation by SMS. Ordered after
        # the organization's settings row, which holds the balance cache.
        from core.provisioning import register_tenant_seeder
        from notifications.seed import seed_opening_sms_credits

        register_tenant_seeder(seed_opening_sms_credits, order=20, name="opening sms credits")

        # T1.20: declares how the A3 isolation suite exercises this app's
        # endpoints. Without it the suite fails rather than skipping them.
        from notifications.isolation import register as register_isolation_fixtures

        register_isolation_fixtures()

        # §9.3: says at startup whether the configured SMS provider can send,
        # instead of letting a missing credential surface as a technician who
        # was never told.
        from notifications import checks  # noqa: F401
