"""Tenant isolation on attachments (§2.3, N-7)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("core", "0006_attachment")]

    operations = [enable_rls("core.Attachment")]
