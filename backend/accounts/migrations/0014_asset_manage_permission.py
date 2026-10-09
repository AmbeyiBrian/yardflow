"""Give existing tenants `asset.manage` (§4.20.7).

Only the Owner holds it by default (the Owner holds every permission), so this
is a sync of the seeded roles: additive, and a tenant that has edited a role
keeps its edits. Reversing is a deliberate no-op, as in 0012.
"""

from django.db import migrations


def sync_roles(apps, schema_editor):  # type: ignore[no-untyped-def]
    from accounts.role_sync import sync_seeded_roles
    from core.tenancy import tenant_context

    Organization = apps.get_model("core", "Organization")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    for organization in Organization.objects.all():
        with tenant_context(organization):
            sync_seeded_roles(
                organization, role_model=Role, permission_model=RolePermission
            )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0013_finance_role"),
        ("core", "0001_initial"),
    ]

    operations = [migrations.RunPython(sync_roles, migrations.RunPython.noop)]
