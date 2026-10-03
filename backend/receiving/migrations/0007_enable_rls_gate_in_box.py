"""Tenant isolation on the draft boxes of a gate-in (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("receiving", "0006_gate_in_boxes")]

    operations = [enable_rls("receiving.GateInBox")]
