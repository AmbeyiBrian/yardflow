"""Tenant isolation on labour entries (§2.3, A3).

They carry a captured day rate, which is the pay-adjacent data O14 exists to
keep out of the wrong hands. A poor table to leave as the exception.
"""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("jobs", "0006_job_labour")]

    operations = [enable_rls("jobs.JobLabour")]
