"""Row-level security on the role and assignment tables (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_role_rolepermission_userrole_and_more")]

    operations = [
        enable_rls(
            "accounts.Role",
            "accounts.RolePermission",
            "accounts.UserRole",
        )
    ]
