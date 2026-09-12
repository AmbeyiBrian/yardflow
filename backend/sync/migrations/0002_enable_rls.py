"""Tenant isolation on sync submissions and exceptions (§2.3, A3).

A submission holds the verbatim payload a device captured — quantities, serials,
sites — so a cross-tenant read here would hand over one organization's field
activity in the rawest form it exists.
"""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("sync", "0001_initial")]

    operations = [
        enable_rls("sync.SyncSubmission", "sync.SyncException")
    ]
