"""Tenant isolation on notifications (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("notifications", "0001_initial")]

    operations = [
        enable_rls("notifications.NotificationEvent", "notifications.NotificationDelivery")
    ]
