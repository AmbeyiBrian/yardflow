"""``GateOut.work_order`` becomes ``GateOut.project`` (§4.7, §4.14; O1, D20).

The exactly-one-destination check names the column, so it cannot survive the
rename: it is dropped first and rebuilt afterwards against ``project_id``. The
rule it enforces is unchanged — a pass with two destinations still cannot be
reconciled against either (`F1`, `H4`).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dispatch", "0003_gate_out_line_no_serial_reason"),
        ("network", "0003_workorder_becomes_project"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="gateout", name="gate_out_has_exactly_one_destination"
        ),
        migrations.RenameField(
            model_name="gateout", old_name="work_order", new_name="project"
        ),
        migrations.AddConstraint(
            model_name="gateout",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("client__isnull", True),
                    ("project__isnull", True),
                    ("site__isnull", False),
                    ("to_location__isnull", True),
                )
                | models.Q(
                    ("client__isnull", True),
                    ("project__isnull", False),
                    ("site__isnull", True),
                    ("to_location__isnull", True),
                )
                | models.Q(
                    ("client__isnull", False),
                    ("project__isnull", True),
                    ("site__isnull", True),
                    ("to_location__isnull", True),
                )
                | models.Q(
                    ("client__isnull", True),
                    ("project__isnull", True),
                    ("site__isnull", True),
                    ("to_location__isnull", False),
                ),
                name="gate_out_has_exactly_one_destination",
            ),
        ),
    ]
