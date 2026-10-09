"""T16.2 — the attendance tables and what they refuse (§4.18.2, R13).

No services here: these pin the constraints, guards and isolation, so a later
service bug cannot write an impossible or already-decided row.
"""

import uuid
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from accounts.factories import UserFactory
from accounts.models import Role
from accounts.permissions_registry import DEFAULT_ROLES, PERM
from approvals.models import ApprovalRequest, ApprovalRequestStatus
from attendance.models import (
    ClosedBy,
    CorrectionKind,
    WorkDay,
    WorkSession,
    WorkSessionCorrection,
)
from core.rls import tables_with_policy
from core.tenancy import tenant_context
from locations.factories import YardFactory
from network.factories import SiteFactory

NOW = timezone.now().replace(microsecond=0)


@pytest.fixture
def person(tenant):
    return UserFactory(organization=tenant)


@pytest.fixture
def site(tenant):
    return SiteFactory()


@pytest.fixture
def work_day(tenant, person):
    return WorkDay.objects.create(organization=tenant, person=person, date=date(2026, 3, 2))


def make_session(tenant, person, work_day, **fields):
    defaults = {
        "organization": tenant,
        "person": person,
        "work_day": work_day,
        "local_date": work_day.date,
        "clock_in_at": NOW,
        "clock_in_received_at": NOW,
        "clock_out_at": NOW + timedelta(hours=8),
    }
    defaults.update(fields)
    return WorkSession.objects.create(**defaults)


def make_request(tenant, work_day, status=ApprovalRequestStatus.PENDING):
    return ApprovalRequest.objects.create(
        organization=tenant,
        document_type="WORK_DAY",
        document_id=str(work_day.pk),
        status=status,
    )


@pytest.mark.django_db
class TestWorkSessionConstraints:
    def test_a_session_at_a_site_saves(self, tenant, person, work_day, site):
        assert make_session(tenant, person, work_day, site=site).pk

    def test_a_session_at_a_location_saves(self, tenant, person, work_day):
        assert make_session(tenant, person, work_day, location=YardFactory()).pk

    def test_a_session_needs_a_place(self, tenant, person, work_day):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_session(tenant, person, work_day)

    def test_a_session_cannot_have_both_places(self, tenant, person, work_day, site):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_session(tenant, person, work_day, site=site, location=YardFactory())

    def test_out_before_in_is_refused(self, tenant, person, work_day, site):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_session(
                tenant, person, work_day, site=site, clock_out_at=NOW - timedelta(minutes=1)
            )

    def test_out_equal_to_in_is_allowed(self, tenant, person, work_day, site):
        assert make_session(tenant, person, work_day, site=site, clock_out_at=NOW).pk

    def test_one_open_session_per_person(self, tenant, person, work_day, site):
        make_session(tenant, person, work_day, site=site, clock_out_at=None)
        with pytest.raises(IntegrityError), transaction.atomic():
            make_session(tenant, person, work_day, site=site, clock_out_at=None)

    def test_a_closed_session_does_not_block_a_new_open_one(
        self, tenant, person, work_day, site
    ):
        make_session(tenant, person, work_day, site=site)
        assert make_session(tenant, person, work_day, site=site, clock_out_at=None).pk

    def test_two_people_can_each_be_open(self, tenant, person, work_day, site):
        other = UserFactory(organization=tenant)
        other_day = WorkDay.objects.create(
            organization=tenant, person=other, date=work_day.date
        )
        make_session(tenant, person, work_day, site=site, clock_out_at=None)
        assert make_session(tenant, other, other_day, site=site, clock_out_at=None).pk

    def test_client_uuids_are_unique_per_org(self, tenant, person, work_day, site):
        key = uuid.uuid4()
        make_session(tenant, person, work_day, site=site, in_client_uuid=key)
        with pytest.raises(IntegrityError), transaction.atomic():
            make_session(tenant, person, work_day, site=site, in_client_uuid=key)

    def test_out_client_uuids_are_unique_per_org(self, tenant, person, work_day, site):
        key = uuid.uuid4()
        make_session(tenant, person, work_day, site=site, out_client_uuid=key)
        with pytest.raises(IntegrityError), transaction.atomic():
            make_session(tenant, person, work_day, site=site, out_client_uuid=key)

    def test_missing_uuids_do_not_collide(self, tenant, person, work_day, site):
        make_session(tenant, person, work_day, site=site)
        assert make_session(tenant, person, work_day, site=site).pk

    def test_defaults(self, tenant, person, work_day, site):
        session = make_session(tenant, person, work_day, site=site)
        assert session.area_changed is False
        assert session.approval_request is None
        assert session.added_by is None
        assert session.closed_by == ""
        assert ClosedBy.AUTO == "AUTO"


@pytest.mark.django_db
class TestWorkDay:
    def test_one_day_per_person_and_date(self, tenant, person, work_day):
        with pytest.raises(IntegrityError), transaction.atomic():
            WorkDay.objects.create(organization=tenant, person=person, date=work_day.date)

    def test_the_engine_aliases_point_at_the_person(self, work_day, person):
        assert work_day.requested_by_id == person.pk
        assert work_day.recorded_by_id == person.pk

    def test_a_new_day_is_open(self, work_day):
        assert work_day.status == "OPEN"


@pytest.mark.django_db
class TestSessionGuard:
    def test_a_pending_session_can_change(self, tenant, person, work_day, site):
        session = make_session(tenant, person, work_day, site=site)
        session.approval_request = make_request(tenant, work_day)
        session.save()
        session = WorkSession.objects.get(pk=session.pk)
        session.added_reason = "edited"
        session.save()

    def test_an_approved_session_cannot_change(self, tenant, person, work_day, site):
        session = make_session(tenant, person, work_day, site=site)
        session.approval_request = make_request(
            tenant, work_day, ApprovalRequestStatus.APPROVED
        )
        session.save()
        session = WorkSession.objects.get(pk=session.pk)
        session.clock_out_at = NOW + timedelta(hours=9)
        with pytest.raises(ValidationError, match="clock_out_at"):
            session.save()

    def test_an_approved_session_cannot_be_deleted(self, tenant, person, work_day, site):
        session = make_session(tenant, person, work_day, site=site)
        session.approval_request = make_request(
            tenant, work_day, ApprovalRequestStatus.APPROVED
        )
        session.save()
        with pytest.raises(ValidationError):
            WorkSession.objects.get(pk=session.pk).delete()

    def test_the_request_cannot_be_swapped_after_approval(
        self, tenant, person, work_day, site
    ):
        session = make_session(tenant, person, work_day, site=site)
        session.approval_request = make_request(
            tenant, work_day, ApprovalRequestStatus.APPROVED
        )
        session.save()
        session = WorkSession.objects.get(pk=session.pk)
        session.approval_request = make_request(tenant, work_day)
        with pytest.raises(ValidationError):
            session.save()

    def test_a_rejected_session_may_only_be_repointed(
        self, tenant, person, work_day, site
    ):
        session = make_session(tenant, person, work_day, site=site)
        session.approval_request = make_request(
            tenant, work_day, ApprovalRequestStatus.REJECTED
        )
        session.save()

        session = WorkSession.objects.get(pk=session.pk)
        session.clock_in_at = NOW - timedelta(hours=1)
        with pytest.raises(ValidationError, match="clock_in_at"):
            session.save()

        session = WorkSession.objects.get(pk=session.pk)
        reopened = make_request(tenant, work_day)
        session.approval_request = reopened
        session.save()
        assert WorkSession.objects.get(pk=session.pk).approval_request_id == reopened.pk

    def test_a_session_just_created_can_be_saved_again(
        self, tenant, person, work_day, site
    ):
        session = make_session(tenant, person, work_day, site=site, clock_out_at=None)
        session.clock_out_at = NOW + timedelta(hours=1)
        session.closed_by = ClosedBy.PERSON
        session.save()


def make_correction(tenant, person, work_day, session=None, **fields):
    defaults = {
        "organization": tenant,
        "work_day": work_day,
        "session": session,
        "kind": CorrectionKind.EDIT,
        "corrected_in_at": NOW,
        "corrected_out_at": NOW + timedelta(hours=7),
        "reason": "Phone was off",
        "made_by": person,
        "made_at": NOW,
    }
    defaults.update(fields)
    return WorkSessionCorrection.objects.create(**defaults)


@pytest.mark.django_db
class TestCorrections:
    def test_an_edit_names_a_session(self, tenant, person, work_day, site):
        session = make_session(tenant, person, work_day, site=site)
        assert make_correction(tenant, person, work_day, session).pk

    def test_an_edit_without_a_session_is_refused(self, tenant, person, work_day):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_correction(tenant, person, work_day)

    def test_an_add_names_one_place_and_no_session(self, tenant, person, work_day, site):
        assert make_correction(
            tenant, person, work_day, kind=CorrectionKind.ADD, site=site
        ).pk

    def test_an_add_without_a_place_is_refused(self, tenant, person, work_day):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_correction(tenant, person, work_day, kind=CorrectionKind.ADD)

    def test_python_refuses_to_edit_a_correction(self, tenant, person, work_day, site):
        session = make_session(tenant, person, work_day, site=site)
        correction = make_correction(tenant, person, work_day, session)
        correction.reason = "changed"
        with pytest.raises(ValueError):
            correction.save()
        with pytest.raises(ValueError):
            correction.delete()

    def test_the_database_refuses_update_and_delete(self, tenant, person, work_day, site):
        session = make_session(tenant, person, work_day, site=site)
        correction = make_correction(tenant, person, work_day, session)
        with pytest.raises(Exception, match="not permitted"), transaction.atomic():
            WorkSessionCorrection.objects.filter(pk=correction.pk).update(reason="x")
        with pytest.raises(Exception, match="not permitted"), transaction.atomic():
            WorkSessionCorrection.objects.filter(pk=correction.pk).delete()


@pytest.mark.django_db
@pytest.mark.rls
class TestIsolation:
    def test_every_table_has_a_policy(self):
        tables = set(tables_with_policy())
        for model in (WorkDay, WorkSession, WorkSessionCorrection):
            assert model._meta.db_table in tables

    def test_raw_sql_sees_only_the_active_tenant(self, organization, other_organization):
        with tenant_context(organization):
            mine = WorkDay.objects.create(
                organization=organization,
                person=UserFactory(organization=organization),
                date=date(2026, 3, 2),
            )
        with tenant_context(other_organization):
            WorkDay.objects.create(
                organization=other_organization,
                person=UserFactory(organization=other_organization),
                date=date(2026, 3, 2),
            )
        with tenant_context(organization), connection.cursor() as cursor:
            cursor.execute("SELECT id FROM attendance_workday")
            assert [row[0] for row in cursor.fetchall()] == [mine.pk]


@pytest.mark.django_db
class TestPermission:
    def test_finance_and_owner_hold_it_by_default(self):
        assert PERM.ATTENDANCE_VIEW_ALL == "attendance.view_all"
        assert PERM.ATTENDANCE_VIEW_ALL in DEFAULT_ROLES["Finance"]
        assert PERM.ATTENDANCE_VIEW_ALL in DEFAULT_ROLES["Owner"]
        assert PERM.ATTENDANCE_VIEW_ALL not in DEFAULT_ROLES["Project manager"]

    def test_a_provisioned_finance_role_has_it(self, tenant):
        from core.provisioning import seed_default_roles

        seed_default_roles(tenant)
        role = Role.objects.get(organization=tenant, name="Finance")
        assert PERM.ATTENDANCE_VIEW_ALL in set(
            role.permissions.values_list("codename", flat=True)
        )

    def test_the_sync_tops_up_an_existing_finance_role(self, tenant):
        from accounts.role_sync import sync_seeded_roles
        from core.provisioning import seed_default_roles

        seed_default_roles(tenant)
        role = Role.objects.get(organization=tenant, name="Finance")
        role.permissions.filter(codename=PERM.ATTENDANCE_VIEW_ALL).delete()

        sync_seeded_roles(tenant)

        assert PERM.ATTENDANCE_VIEW_ALL in set(
            role.permissions.values_list("codename", flat=True)
        )
