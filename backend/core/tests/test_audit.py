"""T1.14 — the audit trail (§4.2, M3, B6)."""

import pytest
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory

from accounts.factories import UserFactory
from core.audit import changed_fields, record, snapshot
from core.models import AuditAction, AuditLog, AuthMethod


class TestAppendOnlyAtTheDatabaseLevel:
    """T1.14: UPDATE and DELETE must raise *at the database level*.

    A Python-only guard protects against the application's own mistakes but not
    against a raw query, a management command or a psql session. An auditor
    needs the stronger statement (M3).
    """

    def test_update_raises_in_the_database(self, tenant):
        entry = record(AuditAction.LOGIN_SUCCEEDED, actor=None, actor_identifier="a@b.com")

        with pytest.raises(IntegrityError, match="not permitted"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    'UPDATE core_auditlog SET note = %s WHERE id = %s',
                    ["tampered", entry.pk],
                )

    def test_delete_raises_in_the_database(self, tenant):
        entry = record(AuditAction.LOGIN_SUCCEEDED, actor=None, actor_identifier="a@b.com")

        with pytest.raises(IntegrityError, match="not permitted"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM core_auditlog WHERE id = %s", [entry.pk])

    def test_the_row_survives_an_attempted_tamper(self, tenant):
        entry = record(AuditAction.LOGIN_SUCCEEDED, actor=None, actor_identifier="a@b.com")

        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM core_auditlog WHERE id = %s", [entry.pk])

        assert AuditLog.objects.filter(pk=entry.pk).exists()

    def test_python_also_refuses_to_modify(self, tenant):
        """Same rule, reported earlier and more clearly."""
        entry = record(AuditAction.LOGIN_SUCCEEDED, actor=None, actor_identifier="a@b.com")

        entry.note = "tampered"
        with pytest.raises(ValueError, match="append-only"):
            entry.save()

    def test_python_also_refuses_to_delete(self, tenant):
        entry = record(AuditAction.LOGIN_SUCCEEDED, actor=None, actor_identifier="a@b.com")

        with pytest.raises(ValueError, match="append-only"):
            entry.delete()


class TestLoginIsRecorded:
    """B6: every login and failed login is recorded."""

    def test_a_successful_login_records_actor_ip_and_user_agent(self, tenant):
        """T1.14's stated criterion, exactly."""
        user = UserFactory(organization=tenant)
        request = RequestFactory().get(
            "/", HTTP_USER_AGENT="Mozilla/5.0 (Android 13)", REMOTE_ADDR="41.90.1.5"
        )

        user_logged_in.send(sender=user.__class__, request=request, user=user)

        entry = AuditLog.objects.get(action=AuditAction.LOGIN_SUCCEEDED)
        assert entry.actor == user
        assert entry.ip == "41.90.1.5"
        assert entry.user_agent == "Mozilla/5.0 (Android 13)"
        assert entry.auth_method == AuthMethod.PASSWORD

    def test_a_failed_login_records_the_attempted_identifier(self, tenant):
        request = RequestFactory().get("/", REMOTE_ADDR="41.90.1.5")

        user_login_failed.send(
            sender=None,
            credentials={"username": "intruder@example.com", "password": "hunter2"},
            request=request,
        )

        entry = AuditLog.objects.get(action=AuditAction.LOGIN_FAILED)
        assert entry.actor is None
        assert entry.actor_identifier == "intruder@example.com"

    def test_a_failed_login_never_records_the_password(self, tenant):
        """The trail is evidence, not a second place to leak credentials."""
        request = RequestFactory().get("/", REMOTE_ADDR="41.90.1.5")

        user_login_failed.send(
            sender=None,
            credentials={"username": "intruder@example.com", "password": "hunter2"},
            request=request,
        )

        entry = AuditLog.objects.get(action=AuditAction.LOGIN_FAILED)
        serialised = f"{entry.before} {entry.after} {entry.note} {entry.actor_identifier}"
        assert "hunter2" not in serialised

    def test_the_forwarded_address_is_preferred_behind_a_load_balancer(self, tenant):
        """§12.1: REMOTE_ADDR would be the ALB, which is useless for an audit."""
        user = UserFactory(organization=tenant)
        request = RequestFactory().get(
            "/", HTTP_X_FORWARDED_FOR="41.90.1.5, 10.0.0.7", REMOTE_ADDR="10.0.0.7"
        )

        user_logged_in.send(sender=user.__class__, request=request, user=user)

        assert AuditLog.objects.get(action=AuditAction.LOGIN_SUCCEEDED).ip == "41.90.1.5"


class TestUnattributableEvents:
    def test_an_event_with_no_organization_is_not_written_to_any_tenant(self, db, caplog):
        """A tenant's trail must never contain another tenant's events.

        A failed login against an unknown subdomain has no organization, so it
        goes to the application log (N-11) rather than being filed arbitrarily.
        """
        entry = record(AuditAction.LOGIN_FAILED, actor_identifier="nobody@example.com")

        assert entry is None
        assert AuditLog.all_objects.count() == 0


class TestSnapshots:
    """M3: before/after values."""

    def test_snapshot_captures_field_values(self, tenant):
        user = UserFactory(organization=tenant, full_name="Jane Storekeeper")

        data = snapshot(user)

        assert data["full_name"] == "Jane Storekeeper"

    def test_snapshot_redacts_the_password(self, tenant):
        user = UserFactory(organization=tenant)

        assert snapshot(user)["password"] == "[redacted]"

    def test_changed_fields_reports_only_what_differs(self):
        before = {"status": "DRAFT", "note": "same"}
        after = {"status": "POSTED", "note": "same"}

        assert changed_fields(before, after) == {
            "status": {"from": "DRAFT", "to": "POSTED"}
        }

    def test_changed_fields_notices_an_added_key(self):
        assert changed_fields({}, {"note": "added"}) == {
            "note": {"from": None, "to": "added"}
        }


class TestAuditIsolation:
    """A3: one tenant cannot read another's audit trail."""

    def test_audit_entries_are_scoped_to_their_organization(
        self, organization, other_organization
    ):
        from core.tenancy import tenant_context

        with tenant_context(organization):
            record(AuditAction.LOGIN_SUCCEEDED, actor_identifier="ours@example.com")
        with tenant_context(other_organization):
            record(AuditAction.LOGIN_SUCCEEDED, actor_identifier="theirs@example.com")

        with tenant_context(organization):
            assert [e.actor_identifier for e in AuditLog.objects.all()] == ["ours@example.com"]
