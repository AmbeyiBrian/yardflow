from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        # Registers the OrganizationSettings invariant (§4.1).
        # Registers the check that every tenant table carries an RLS policy
        # (§2.3, A3).
        # Teaches drf-spectacular about this project's authentication class, so
        # the schema generates without warnings (N-10, T1.21).
        from core import (
            checks,  # noqa: F401
            schema,  # noqa: F401
            signals,  # noqa: F401
        )

        # T1.20: how the A3 suite exercises the attachment endpoint.
        from core.isolation_fixtures import register as register_isolation_fixtures

        register_isolation_fixtures()

        # N-11: error tracking, if a DSN is configured. Silent otherwise.
        from core.logging import configure_error_tracking

        configure_error_tracking()
