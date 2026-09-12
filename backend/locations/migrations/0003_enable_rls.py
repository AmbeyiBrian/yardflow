"""Tenant isolation on locations and stock nodes (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [
        # 0002_initial adds the StockNode foreign keys to Site and Client, so it
        # must land before the policies are applied.
        ("locations", "0002_initial"),
        ("network", "0001_initial"),
    ]

    operations = [enable_rls("locations.Location", "locations.StockNode")]
