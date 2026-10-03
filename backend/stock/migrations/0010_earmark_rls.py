"""Tenant isolation on earmarks, and the append-only trail (§4.16.2)."""

from django.db import migrations

from core.immutability import make_append_only
from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("stock", "0009_earmarks")]

    operations = [
        enable_rls("stock.BulkEarmark", "stock.EarmarkEvent"),
        make_append_only(
            "stock.EarmarkEvent",
            "earmark events are append-only; the trail of where material was meant to go is never edited",
        ),
    ]
