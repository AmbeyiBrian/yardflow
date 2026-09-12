"""Tenant isolation on approvals, and append-only approval actions (§2.3, §4.8, M3)."""

from django.db import migrations

from core.immutability import make_append_only
from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("approvals", "0001_initial")]

    operations = [
        enable_rls("approvals.ApprovalRule", "approvals.ApprovalRequest", "approvals.ApprovalAction"),
        make_append_only(
            "approvals.ApprovalAction",
            "an approval action is the non-repudiation evidence an auditor asks "
            "for and cannot be changed once recorded (M3)",
        ),
    ]
