"""Give in-flight expenses their approval requests (§4.17.11; R4).

0003 moved ``SUBMITTED`` expenses to ``PENDING_PM`` without creating requests,
because the approval tables did not yet carry a permission level. Now they do,
and the status is meant to be a projection of the requests (§4.17.1), so each
``PENDING_PM`` expense gets the levels it would have been routed to today:

* level 1, the project's manager, unless the recorder is that manager or holds
  the tenant's Finance Director role (then the level is skipped and the entry
  waits at level 2 alone, and its status moves to ``PENDING_FINANCE`` so the
  status still reads as its requests do, §4.17.3);
* level 2, whoever holds ``finance.approve``.

An expense whose project has no manager is **left without requests**: there is
nobody to address level 1 to, and routing it round the PM would weaken the
control (D28). It stays ``PENDING_PM`` until an owner assigns a manager.

Idempotent: an expense that already has any request is skipped, so re-running it
(or running it after the engine has created some) adds nothing.

Reversing is a no-op: the requests may already carry decisions.
"""

from django.db import migrations

#: Frozen copies: a migration must not follow later edits to the engine.
EXPENSE_LABEL = "commercials.ProjectExpense"
FINANCE_PERMISSION = "finance.approve"


def create_requests(apps, schema_editor):  # type: ignore[no-untyped-def]
    from core.tenancy import tenant_context

    Organization = apps.get_model("core", "Organization")
    OrganizationSettings = apps.get_model("core", "OrganizationSettings")
    ProjectExpense = apps.get_model("commercials", "ProjectExpense")
    ApprovalRequest = apps.get_model("approvals", "ApprovalRequest")
    UserRole = apps.get_model("accounts", "UserRole")

    for organization in Organization.objects.all():
        with tenant_context(organization):
            settings = OrganizationSettings.objects.filter(organization=organization).first()
            director_role_id = settings.finance_director_role_id if settings else None

            pending = ProjectExpense.objects.filter(
                organization=organization, status="PENDING_PM"
            ).select_related("project")

            for expense in pending:
                already = ApprovalRequest.objects.filter(
                    organization=organization,
                    document_type=EXPENSE_LABEL,
                    document_id=str(expense.pk),
                ).exists()
                if already:
                    continue

                manager_id = expense.project.manager_id
                if manager_id is None:
                    continue

                skip_pm = manager_id == expense.recorded_by_id or (
                    director_role_id is not None
                    and UserRole.objects.filter(
                        organization=organization,
                        user_id=expense.recorded_by_id,
                        role_id=director_role_id,
                    ).exists()
                )

                if skip_pm:
                    ProjectExpense.objects.filter(pk=expense.pk).update(
                        status="PENDING_FINANCE"
                    )
                else:
                    ApprovalRequest.objects.create(
                        organization=organization,
                        document_type=EXPENSE_LABEL,
                        document_id=str(expense.pk),
                        level=1,
                        required_user_id=manager_id,
                        requested_by_id=expense.recorded_by_id,
                    )
                ApprovalRequest.objects.create(
                    organization=organization,
                    document_type=EXPENSE_LABEL,
                    document_id=str(expense.pk),
                    level=2,
                    required_permission=FINANCE_PERMISSION,
                    requested_by_id=expense.recorded_by_id,
                )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0013_finance_role"),
        ("approvals", "0006_request_required_permission"),
        ("commercials", "0005_seed_finance_categories"),
        ("core", "0017_finance_settings_attachment_fields"),
    ]

    operations = [migrations.RunPython(create_requests, migrations.RunPython.noop)]
