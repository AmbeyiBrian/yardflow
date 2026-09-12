"""Tenant isolation on the catalogue (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("catalogue", "0001_initial")]

    operations = [
        enable_rls(
            "catalogue.ItemCategory",
            "catalogue.CategoryCustomField",
            "catalogue.ItemType",
        )
    ]
