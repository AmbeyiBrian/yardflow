"""Row-level security on the test tenant table (§2.3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("tenancy_app", "0001_initial")]

    operations = [enable_rls("tenancy_app.Widget")]
