"""Tenant isolation on clients, sites and work orders (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("network", "0001_initial")]

    operations = [
        enable_rls(
            "network.Client",
            "network.Site",
            "network.SiteReference",
            "network.WorkOrder",
        )
    ]
