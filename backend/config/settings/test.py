"""Test settings.

Postgres is required, not optional: the tenancy layer relies on row-level
security, and the ledger and audit trail rely on database triggers (§2.3,
§3.2). SQLite cannot express any of that, so tests would pass while the real
guarantees went unverified.
"""

from config.settings.base import *
from config.settings.base import env  # noqa: F401

DEBUG = False

# Long enough to satisfy HS256's recommended key length, so the JWT library
# does not warn on every token issued in the suite.
SECRET_KEY = "test-only-secret-key-padded-to-thirty-two-bytes-plus"

ALLOWED_HOSTS = [".localhost", "testserver", "127.0.0.1"]

# Faster hashing; irrelevant to what these tests assert.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Run tasks inline so tests assert on outcomes, not on queue mechanics.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

SMS_BACKEND = "notifications.channels.logging_sms.LoggingSmsBackend"

# A concrete TenantModel to test the tenancy layers against (§2.1, §2.3).
INSTALLED_APPS = [*INSTALLED_APPS, "core.tests.tenancy_app"]
