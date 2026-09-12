"""Tenant isolation on the numbering counters (§2.3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("core", "0004_documentsequence")]

    operations = [enable_rls("core.DocumentSequence")]
