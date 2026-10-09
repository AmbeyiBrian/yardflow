"""Tenant isolation on the supplier register (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("network", "0011_supplier_register")]

    operations = [enable_rls("network.Supplier")]
