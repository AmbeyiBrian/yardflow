"""Tenant isolation on WebAuthn credentials (§2.3, A3)."""

from django.db import migrations

from core.rls import enable_rls


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_webauthncredential")]

    operations = [enable_rls("accounts.WebAuthnCredential")]
