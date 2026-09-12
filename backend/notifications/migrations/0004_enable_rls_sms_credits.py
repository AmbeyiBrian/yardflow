"""Tenant isolation on the SMS credit ledger (§2.3, A3, L4).

Money, even in credits, is the last thing that should be readable across
tenants — and the system check that fails startup when a tenant table has no
policy would have caught it anyway.
"""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("notifications", "0003_smscreditentry")]

    operations = [enable_rls("notifications.SmsCreditEntry")]
