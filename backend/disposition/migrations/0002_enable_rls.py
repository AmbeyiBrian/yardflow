"""Tenant isolation on dispositions, disposals and return acknowledgements (§2.3, A3).

A disposal record is evidence of a write-off and a return acknowledgement is
evidence that liability ended — both are exactly the rows a competitor must never
read, so they carry the same row-level policy as everything else.
"""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("disposition", "0001_initial")]

    operations = [
        enable_rls(
            "disposition.Disposition",
            "disposition.DispositionLine",
            "disposition.Disposal",
            "disposition.DisposalLine",
            "disposition.ClientReturnAck",
        )
    ]
