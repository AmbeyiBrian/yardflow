"""``ProjectSite.organization`` becomes NOT NULL; tenant isolation (A3)."""

import django.db.models.deletion
import django.db.models.manager
from django.db import migrations, models

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("network", "0014_projectsite_backfill")]

    operations = [
        migrations.AlterField(
            model_name="projectsite",
            name="organization",
            field=models.ForeignKey(editable=False, on_delete=django.db.models.deletion.PROTECT, related_name="%(app_label)s_%(class)s_set", to="core.organization"),
        ),
        migrations.AlterModelManagers(
            name="projectsite",
            managers=[
                ("objects", django.db.models.manager.Manager()),
                ("all_objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.AddConstraint(
            model_name="projectsite",
            constraint=models.UniqueConstraint(fields=("project", "site"), name="uniq_project_site"),
        ),
        enable_rls("network.ProjectSite"),
    ]
