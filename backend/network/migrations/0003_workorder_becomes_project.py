"""WorkOrder becomes Project (§4.4, §4.14; O1, D20).

Written by hand rather than generated: the autodetector reads a rename as a
drop and a create, which would discard every row and every foreign key pointing
at them. ``RenameModel`` renames the table in place and leaves the row-level
security policy attached to it — Postgres carries policies through
``ALTER TABLE ... RENAME``, so §2.3's guarantee is not interrupted.

The FK **columns** on ``jobs_job`` and ``dispatch_gateout`` are not touched here.
They are still named ``work_order_id`` after this migration and are renamed by
each app's own migration, which depends on this one.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    # Renaming a model other apps point at has to happen *after* the migrations
    # that create those foreign keys. Without these, a fresh database is free to
    # order the rename first, and dispatch.0001 then builds a key to a model
    # the state no longer has.
    dependencies = [
        ("network", "0002_enable_rls"),
        ("jobs", "0002_enable_rls"),
        ("dispatch", "0003_gate_out_line_no_serial_reason"),
    ]

    operations = [
        migrations.RenameModel(old_name="WorkOrder", new_name="Project"),
        # Constraint names carry the old noun. Postgres keeps them through the
        # table rename, so they are dropped by their old names and added back
        # under the new ones.
        migrations.RemoveConstraint(
            model_name="project", name="uniq_work_order_ref_per_org"
        ),
        migrations.RemoveConstraint(
            model_name="project", name="closed_work_order_has_a_closing_time"
        ),
        migrations.AddConstraint(
            model_name="project",
            constraint=models.UniqueConstraint(
                fields=("organization", "reference"), name="uniq_project_ref_per_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="project",
            constraint=models.CheckConstraint(
                condition=models.Q(("closed_at__isnull", True), ("status", "OPEN"))
                | models.Q(("closed_at__isnull", False), ("status", "CLOSED")),
                name="closed_project_has_a_closing_time",
            ),
        ),
    ]
