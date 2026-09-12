"""Give tenants that predate credits an opening balance (L4).

Without this, introducing metering switches SMS off for every tenant already
using it — silently, since the symptom is messages that simply stop arriving.
That is the failure L4 exists to prevent, so it should not be the way L4 arrives.

A ledger entry rather than a number, like every other credit: an administrator
looking at the balance later can see exactly where it came from.
"""

from django.db import migrations

#: Enough to keep a yard running while a first purchase is arranged, and small
#: enough that nobody mistakes it for a gift.
OPENING_BALANCE = 100


def grant_opening_balance(apps, schema_editor):
    Organization = apps.get_model("core", "Organization")
    OrganizationSettings = apps.get_model("core", "OrganizationSettings")
    SmsCreditEntry = apps.get_model("notifications", "SmsCreditEntry")

    for organization in Organization.objects.all():
        settings_row = OrganizationSettings.objects.filter(
            organization=organization
        ).first()
        if settings_row is None or settings_row.sms_credit_balance != 0:
            continue

        SmsCreditEntry.objects.create(
            organization=organization,
            kind="ADJUSTMENT",
            quantity=OPENING_BALANCE,
            note="Opening balance when SMS credits were introduced.",
        )
        settings_row.sms_credit_balance = OPENING_BALANCE
        settings_row.save(update_fields=["sms_credit_balance"])


def remove_opening_balance(apps, schema_editor):
    """Reversible, so the migration can be rolled back cleanly in development."""
    SmsCreditEntry = apps.get_model("notifications", "SmsCreditEntry")
    OrganizationSettings = apps.get_model("core", "OrganizationSettings")

    granted = SmsCreditEntry.objects.filter(
        note="Opening balance when SMS credits were introduced."
    )
    for entry in granted:
        settings_row = OrganizationSettings.objects.filter(
            organization_id=entry.organization_id
        ).first()
        if settings_row is not None:
            settings_row.sms_credit_balance -= entry.quantity
            settings_row.save(update_fields=["sms_credit_balance"])
    granted.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0004_enable_rls_sms_credits"),
        ("core", "0011_organizationsettings_sms_credit_balance_and_more"),
    ]

    operations = [migrations.RunPython(grant_opening_balance, remove_opening_balance)]
