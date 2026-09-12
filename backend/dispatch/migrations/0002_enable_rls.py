"""Tenant isolation on the dispatch documents (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("dispatch", "0001_initial")]

    operations = [
        enable_rls(
            "dispatch.GateOut",
            "dispatch.GateOutLine",
            "dispatch.GateOutLineSerial",
            "dispatch.GateOutLineReel",
            "dispatch.ReleaseVariance",
        )
    ]
