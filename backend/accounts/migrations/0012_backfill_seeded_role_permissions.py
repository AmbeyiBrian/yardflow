"""Give existing tenants the permissions added since they were provisioned (§4.2).

`DEFAULT_ROLES` is applied at provisioning. Every permission added afterwards —
Epic O's five, and `job.manage` — reached only tenants created since, and the
symptom was silence: an owner on a screen that renders what they may see got a
page with the figures missing and no error to explain it.

This backfills every tenant that already exists. It is additive, so a tenant
that edited its own roles keeps those edits.

Reversing it is a deliberate no-op. The permissions it grants are the ones the
registry says these roles should have had all along, and taking them away again
would recreate the bug rather than undo a change.
"""

from django.db import migrations


def backfill(apps, schema_editor):  # type: ignore[no-untyped-def]
    from accounts.role_sync import sync_seeded_roles

    Organization = apps.get_model("core", "Organization")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    for organization in Organization.objects.all():
        sync_seeded_roles(
            organization, role_model=Role, permission_model=RolePermission
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0011_day_rates"),
        ("core", "0001_initial"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
