"""Tenant isolation on project variations (§2.3, A3).

Every tenant table carries the policy. A variation holds contract values, which
is among the most commercially sensitive data in the system (`O14`), so it would
be a poor one to leave as the exception.
"""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("network", "0006_project_variation")]

    operations = [enable_rls("network.ProjectVariation")]
