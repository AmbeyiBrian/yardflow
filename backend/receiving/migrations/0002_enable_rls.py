"""Tenant isolation on the receiving documents (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("receiving", "0001_initial")]

    operations = [
        enable_rls(
            "receiving.GateIn",
            "receiving.GateInLine",
            "receiving.GateInSerial",
            "receiving.GateInReel",
        )
    ]
