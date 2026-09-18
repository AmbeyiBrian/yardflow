"""``Job.work_order`` becomes ``Job.project`` (§4.9, §4.14; O1, D20)."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0002_enable_rls"),
        ("network", "0003_workorder_becomes_project"),
    ]

    operations = [
        migrations.RenameField(
            model_name="job", old_name="work_order", new_name="project"
        ),
    ]
