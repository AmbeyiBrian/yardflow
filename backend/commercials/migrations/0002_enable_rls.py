"""Tenant isolation on the commercial tables (§2.3, A3).

Expenses and snapshots carry contract values and margins — the most
commercially sensitive data in the system (`O14`). A poor place to make an
exception to the rule that every tenant table carries the policy.
"""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("commercials", "0001_initial")]

    operations = [
        enable_rls(
            "commercials.ExpenseCategory",
            "commercials.ProjectExpense",
            "commercials.ProjectSnapshot",
        )
    ]
