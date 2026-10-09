"""Give existing tenants the Finance role and `finance.approve` (§4.17.7).

``sync_seeded_roles`` only tops up roles that exist, so the new seeded role is
created here first, then every seeded role (the Owner, which holds everything)
is synced. Additive: a tenant that already has a role named Finance keeps it
exactly as it is, permissions and all.

Reversing is a deliberate no-op, as in 0012.
"""

from django.db import migrations

ROLE_NAME = "Finance"
PERMISSIONS = ("finance.approve", "project.view_cost", "report.view_all")


def add_finance_role(apps, schema_editor):  # type: ignore[no-untyped-def]
    from accounts.role_sync import sync_seeded_roles
    from core.tenancy import tenant_context

    Organization = apps.get_model("core", "Organization")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    for organization in Organization.objects.all():
        with tenant_context(organization):
            role, created = Role.objects.get_or_create(
                organization=organization,
                name=ROLE_NAME,
                defaults={"is_system": True},
            )
            if created:
                RolePermission.objects.bulk_create(
                    [
                        RolePermission(
                            organization_id=organization.pk, role=role, codename=codename
                        )
                        for codename in PERMISSIONS
                    ]
                )
        sync_seeded_roles(organization, role_model=Role, permission_model=RolePermission)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0012_backfill_seeded_role_permissions"),
        ("core", "0001_initial"),
    ]

    operations = [migrations.RunPython(add_finance_role, migrations.RunPython.noop)]
