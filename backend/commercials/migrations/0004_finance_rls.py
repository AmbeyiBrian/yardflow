"""Tenant isolation on the finance tables (§2.3, A3; §4.17.2).

A casual's ID number and an allowance request's amount are as private as an
expense, and the same rule applies: every tenant table carries the policy.
"""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("commercials", "0003_finance_models")]

    operations = [
        enable_rls(
            "commercials.AllowanceRequest",
            "commercials.Casual",
            "commercials.ExpenseCasualLine",
        )
    ]
