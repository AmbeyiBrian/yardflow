"""Tenant isolation on stock counts (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("stock", "0003_stockcount_stockcountline_and_more")]

    operations = [enable_rls("stock.StockCount", "stock.StockCountLine")]
