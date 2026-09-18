"""Tenant isolation on the subcontractor register (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("network", "0008_subcontractor")]

    operations = [enable_rls("network.Subcontractor")]
