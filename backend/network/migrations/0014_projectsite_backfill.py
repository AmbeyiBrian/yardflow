"""Backfill ``ProjectSite.organization`` from its project; stamp existing POs."""

from django.db import migrations
from django.db.models import F

from core.rls import rls_bypass


def forwards(apps, schema_editor):  # type: ignore[no-untyped-def]
    # Row-level security hides every row when no organization is in context.
    with rls_bypass():
        _backfill(apps, schema_editor)


def _backfill(apps, schema_editor):  # type: ignore[no-untyped-def]
    schema_editor.execute(
        "UPDATE network_project_sites ps SET organization_id = p.organization_id "
        "FROM network_project p WHERE p.id = ps.project_id "
        "AND ps.organization_id IS NULL"
    )
    Project = apps.get_model("network", "Project")
    Project.objects.filter(po_number__gt="", po_recorded_at__isnull=True).update(
        po_recorded_at=F("opened_at")
    )


class Migration(migrations.Migration):
    dependencies = [("network", "0013_projectsite_through_model")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
