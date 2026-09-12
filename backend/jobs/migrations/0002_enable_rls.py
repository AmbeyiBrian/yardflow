"""Tenant isolation on jobs, closeouts and variances (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("jobs", "0001_initial")]

    operations = [
        enable_rls(
            "jobs.Job",
            "jobs.JobCloseout",
            "jobs.JobCloseoutLine",
            "jobs.Variance",
        )
    ]
