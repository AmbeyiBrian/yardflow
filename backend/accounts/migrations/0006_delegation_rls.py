"""Tenant isolation on delegations (§2.3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_delegation")]

    operations = [enable_rls("accounts.Delegation")]
