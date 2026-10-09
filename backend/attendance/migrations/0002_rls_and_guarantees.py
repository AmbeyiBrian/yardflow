"""Tenant isolation and the correction ledger's guarantee (§4.18.2, A3).

Corrections are the evidence of what a person said and when, so the table
refuses UPDATE and DELETE in the database, not only in Python.
"""

from django.db import migrations

from core.immutability import make_append_only
from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("attendance", "0001_work_sessions")]

    operations = [
        enable_rls(
            "attendance.WorkDay",
            "attendance.WorkSession",
            "attendance.WorkSessionCorrection",
        ),
        make_append_only(
            "attendance.WorkSessionCorrection",
            "a correction is never edited; add another correction row instead "
            "(§4.18.6)",
        ),
    ]
