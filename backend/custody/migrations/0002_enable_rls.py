"""Tenant isolation on custody records (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("custody", "0001_initial")]

    operations = [
        enable_rls(
            "custody.CustodyExpectation",
            "custody.CustodyTransfer",
            "custody.CustodyTransferLine",
        )
    ]
