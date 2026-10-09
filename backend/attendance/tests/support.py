"""Builders shared by the T16.6, T16.7 and T16.9 tests."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from attendance.models import ClosedBy, WorkDay, WorkSession
from network.factories import ProjectFactory

NAIROBI = ZoneInfo("Africa/Nairobi")
MONDAY = date(2026, 3, 2)


def at(day: date, hour: int, minute: int = 0, zone: ZoneInfo = NAIROBI) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=zone)


def make_director_role(tenant):
    role = RoleFactory(name="Director")
    tenant.settings.finance_director_role = role
    tenant.settings.save()
    return role


def make_user(tenant, name, role=None):
    user = UserFactory(organization=tenant, full_name=name)
    if role is not None:
        UserRoleFactory(user=user, role=role)
    return user


def make_owner(tenant):
    role = RoleFactory(name="Owner", codenames=[PERM.USERS_MANAGE])
    return make_user(tenant, "Olive Owner", role)


def make_project(site, manager=None):
    project = ProjectFactory(manager=manager)
    project.sites.add(site, through_defaults={"organization_id": project.organization_id})
    return project


def make_session(
    tenant,
    person,
    site,
    day: date = MONDAY,
    start: int = 8,
    end: int | None = 12,
    project=None,
    zone: ZoneInfo = NAIROBI,
    **extra,
) -> WorkSession:
    """A session on ``day`` local, closed by the person unless ``end`` is None."""
    work_day, _ = WorkDay.objects.get_or_create(
        organization_id=tenant.pk, person=person, date=day
    )
    clock_in = at(day, start, zone=zone)
    values = {
        "organization_id": tenant.pk,
        "person": person,
        "site": site,
        "project": project,
        "work_day": work_day,
        "local_date": day,
        "clock_in_at": clock_in,
        "clock_in_received_at": clock_in,
    }
    if end is not None:
        values.update(
            clock_out_at=at(day, end, zone=zone),
            clock_out_received_at=at(day, end, zone=zone),
            closed_by=ClosedBy.PERSON,
        )
    values.update(extra)
    return WorkSession.objects.create(**values)
