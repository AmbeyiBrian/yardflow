from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    """Clock-in: work sessions, work days and their corrections (§4.18, R13)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "attendance"

    def ready(self) -> None:
        from attendance import isolation

        isolation.register()
