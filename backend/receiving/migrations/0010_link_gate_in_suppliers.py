"""R15: link old gate-ins to register suppliers by name key (§4.20.5).

A no-op while the register is empty. Never creates suppliers and never
overwrites a ``supplier`` already set.
"""

from django.db import migrations

from core.rls import rls_bypass


def _key(value):
    # Same rule as network.Supplier.normalise_name.
    return " ".join(value.casefold().split())


def link(apps, schema_editor):
    Supplier = apps.get_model("network", "Supplier")
    GateIn = apps.get_model("receiving", "GateIn")
    with rls_bypass():
        by_key = {(s.organization_id, s.name_key): s.pk for s in Supplier._base_manager.all()}
        if not by_key:
            return
        rows = GateIn._base_manager.filter(supplier__isnull=True).exclude(supplier_name="")
        for pk, org_id, name in rows.values_list("pk", "organization_id", "supplier_name"):
            supplier_id = by_key.get((org_id, _key(name)))
            if supplier_id:
                GateIn._base_manager.filter(pk=pk, supplier__isnull=True).update(
                    supplier_id=supplier_id
                )


class Migration(migrations.Migration):
    dependencies = [
        ("network", "0012_supplier_rls"),
        ("receiving", "0009_supplier_register"),
    ]

    operations = [migrations.RunPython(link, migrations.RunPython.noop)]
