"""Give existing tenants the four R1 expense categories (§4.17.11).

Existing categories already read ``kind=GENERAL`` (the column default), and
"Transport and fuel" stays as it is. Only categories missing by name are added:
a tenant that already made its own "Fuel" keeps it as a GENERAL category rather
than having its meaning changed under it.

Reversing is a no-op: an expense may already point at a category added here.
"""

from django.db import migrations

#: Frozen copy: a migration must not follow later edits to the live list.
NEW_CATEGORIES = (
    ("Fuel", "FUEL", "FUEL"),
    ("Team allowance", "TEAM_ALLOWANCE", "GENERAL"),
    ("Transport", "TRANSPORT_FARE", "GENERAL"),
    ("Casual labour", "CASUAL_LABOUR", "CASUAL_LABOUR"),
)


def add_categories(apps, schema_editor):  # type: ignore[no-untyped-def]
    from core.tenancy import tenant_context

    Organization = apps.get_model("core", "Organization")
    ExpenseCategory = apps.get_model("commercials", "ExpenseCategory")

    for organization in Organization.objects.all():
        with tenant_context(organization):
            for name, code, kind in NEW_CATEGORIES:
                ExpenseCategory.objects.get_or_create(
                    organization=organization,
                    name=name,
                    defaults={"code": code, "kind": kind},
                )


class Migration(migrations.Migration):
    dependencies = [
        ("commercials", "0004_finance_rls"),
        ("core", "0001_initial"),
    ]

    operations = [migrations.RunPython(add_categories, migrations.RunPython.noop)]
