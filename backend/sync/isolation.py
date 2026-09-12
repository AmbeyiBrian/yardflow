"""Isolation fixtures for the sync endpoints (T1.20, A3).

A submission holds the verbatim payload a device captured — quantities, serials,
sites — so a cross-tenant read here would hand over another organization's field
activity in the rawest form it exists anywhere in the system.
"""

from uuid import uuid4

from core.isolation import register_isolation_fixture


def register() -> None:
    from sync.models import SubmissionStatus, SyncException, SyncOperation, SyncSubmission

    def make_submission(organization):
        return SyncSubmission.objects.create(
            organization=organization,
            client_uuid=uuid4(),
            operation=SyncOperation.GATE_IN,
            status=SubmissionStatus.APPLIED,
            payload={"supplier_name": "Isolation supplier"},
        )

    def make_exception(organization):
        return SyncException.objects.create(
            organization=organization,
            submission=make_submission(organization),
            code="ISOLATION",
            reason="Isolation fixture.",
        )

    # Neither accepts a PATCH: a submission is a record of what a device sent,
    # and an exception is resolved through its action.
    register_isolation_fixture("sync-submission", make_submission)
    register_isolation_fixture("sync-exception", make_exception)
