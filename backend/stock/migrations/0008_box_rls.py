"""Tenant isolation on boxes, and the append-only trail (§4.15.2, P8, A3)."""

from django.db import migrations

from core.immutability import make_append_only
from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("stock", "0007_boxes")]

    operations = [
        enable_rls("stock.Box", "stock.BoxBulkContent", "stock.BoxEvent"),
        make_append_only(
            "stock.BoxEvent",
            "box events are append-only; the trail of what was in a box is never edited (P8)",
        ),
    ]
