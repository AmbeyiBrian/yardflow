"""Tenant isolation on the asset register, and append-only handovers (§2.3, §4.20.2)."""

from django.db import migrations

from core.immutability import make_append_only
from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("assets", "0001_asset_register")]

    operations = [
        enable_rls("assets.Asset", "assets.AssetHandover"),
        make_append_only(
            "assets.AssetHandover",
            "a handover is the record of who held an asset and cannot be changed "
            "once recorded; hand it over again to correct it (R14)",
        ),
    ]
