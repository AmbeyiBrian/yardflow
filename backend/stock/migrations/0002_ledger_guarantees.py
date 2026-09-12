"""Tenant isolation and append-only enforcement on the ledger (§2.3, §3.2, M3).

The append-only trigger is the load-bearing part. A Python-only guard protects
against the application's own mistakes; the trigger protects the ledger against
a raw query, a management command, a data migration or a psql session — which is
the guarantee that lets every stock figure be *derived* from movements.
"""

from django.db import migrations

from core.immutability import make_append_only
from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("stock", "0001_initial")]

    operations = [
        enable_rls(
            "stock.StockMovement",
            "stock.StockBalance",
            "stock.SerialUnit",
            "stock.Reel",
        ),
        make_append_only(
            "stock.StockMovement",
            "the stock ledger is append-only; correct it by posting a REVERSAL "
            "movement instead (M4)",
        ),
    ]
