"""Give existing tenants `attendance.view_all` on the Finance role (§4.18.8).

``sync_seeded_roles`` adds what a seeded role is missing from its default:
here the Finance role and the Owner (which holds everything). Additive and
idempotent, so a customised role keeps what it has. Reversing is a deliberate
no-op, as in 0012.
"""

from django.db import migrations


def add_permission(apps, schema_editor):  # type: ignore[no-untyped-def]
    from accounts.role_sync import sync_seeded_roles

    Organization = apps.get_model("core", "Organization")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    # The sync grants every default a seeded role lacks, which now includes
    # `attendance.view_all` on Finance and Owner.
    for organization in Organization.objects.all():
        sync_seeded_roles(organization, role_model=Role, permission_model=RolePermission)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0014_asset_manage_permission"),
        ("core", "0001_initial"),
    ]

    operations = [migrations.RunPython(add_permission, migrations.RunPython.noop)]
