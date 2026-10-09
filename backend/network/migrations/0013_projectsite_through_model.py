"""R10, R12: ``Project.sites`` gets a through model; the PO gets its columns.

State only for the join table: ``ProjectSite`` is declared over the table the
plain many-to-many already made (``network_project_sites``), so no link is
copied or lost. ``organization`` is added nullable here, backfilled in 0011 and
made NOT NULL in 0012 (separate transactions: Postgres refuses to alter a table
with pending foreign-key trigger events).
"""

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_organizationsettings_sms_credit_balance_and_more"),
        ("accounts", "0001_initial"),
        ("network", "0012_supplier_rls"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="ProjectSite",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="project_sites", to="network.project")),
                        ("site", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="site_projects", to="network.site")),
                    ],
                    options={"db_table": "network_project_sites"},
                ),
                migrations.AlterField(
                    model_name="project",
                    name="sites",
                    field=models.ManyToManyField(blank=True, related_name="projects", through="network.ProjectSite", to="network.site"),
                ),
            ],
        ),
        migrations.AddField(
            model_name="projectsite",
            name="organization",
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="%(app_label)s_%(class)s_set", to="core.organization"),
        ),
        migrations.AddField(
            model_name="projectsite",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, db_index=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="projectsite",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="projectsite",
            name="created_by",
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="accounts.user"),
        ),
        migrations.AddField(
            model_name="projectsite",
            name="mobilised_on",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="projectsite",
            name="accepted_on",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="project",
            name="po_issue_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="project",
            name="payment_terms",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="project",
            name="payment_terms_days",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="project",
            name="po_recorded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
