"""The audit trail is append-only in the database, not just in Python (M3)."""

from django.db import migrations

from core.immutability import make_append_only
from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("core", "0002_auditlog")]

    operations = [
        enable_rls("core.AuditLog"),
        make_append_only(
            "core.AuditLog",
            "the audit trail is append-only and cannot be edited or deleted by "
            "any tenant user (M3)",
        ),
    ]
