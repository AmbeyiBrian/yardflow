"""Isolation fixtures for the attendance tables (T16.2, A3).

No endpoints exist yet (later tasks); the fixtures are registered now so the
viewsets that arrive are covered by the A3 suite from their first commit.
"""

from core.isolation import register_isolation_fixture


def register() -> None:
    from datetime import date

    from django.utils import timezone

    from accounts.models import User

    def make_work_day(organization):
        from attendance.models import WorkDay

        person = User.objects.filter(organization=organization).first()
        return WorkDay.objects.create(
            organization=organization, person=person, date=date(2026, 1, 1)
        )

    def make_work_session(organization):
        from attendance.models import WorkSession
        from network.models import Client, Site

        day = make_work_day(organization)
        client = Client.objects.create(organization=organization, name="Isolation Op")
        site = Site.objects.create(
            organization=organization,
            client=client,
            internal_ref="ISO-ATT-1",
            name="Isolation site",
        )
        now = timezone.now()
        return WorkSession.objects.create(
            organization=organization,
            person=day.person,
            site=site,
            work_day=day,
            local_date=day.date,
            clock_in_at=now,
            clock_in_received_at=now,
            clock_out_at=now,
        )

    register_isolation_fixture("work-day", make_work_day)
    register_isolation_fixture("work-session", make_work_session)
