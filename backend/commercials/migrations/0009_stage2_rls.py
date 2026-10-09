"""Tenant isolation on the stage-2 finance tables (§2.3, A3; §4.19.2)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("commercials", "0008_stage2_models")]

    operations = [
        enable_rls(
            "commercials.SitePurchase",
            "commercials.SitePurchaseLine",
            "commercials.Subcontract",
            "commercials.SubcontractPayment",
            "commercials.ProjectMilestone",
            "commercials.MilestoneInvoice",
            "commercials.MilestoneReceipt",
        )
    ]
