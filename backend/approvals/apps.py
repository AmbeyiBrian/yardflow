from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'approvals'

    def ready(self) -> None:
        # A new tenant starts with approval routing that does something (F3).
        # Ordered after the seeded roles, which the rule points at.
        from approvals.seed import seed_default_approval_rules
        from core.provisioning import register_tenant_seeder

        register_tenant_seeder(
            seed_default_approval_rules, order=40, name="approval rules"
        )

        # T1.20: declares how the A3 isolation suite exercises this app's
        # endpoints. Without it the suite fails rather than skipping them.
        from approvals.isolation import register as register_isolation_fixtures

        register_isolation_fixtures()
