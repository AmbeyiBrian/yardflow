"""Local development settings.

Design §12.0: the whole build runs locally. No AWS account, no cloud
credentials, no managed services. Files go to local disk, email to the console,
SMS to the log.
"""

from config.settings.base import *
from config.settings.base import BASE_DIR, env

DEBUG = True

# Insecure by design and only ever used here. Production requires a real value
# and refuses to boot without one (see prod.py).
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="dev-insecure-do-not-use-outside-local-development",
)

# Tenants are addressed by subdomain, so every *.localhost name must resolve
# (§2.2, §12.0). Modern browsers send *.localhost to loopback with no
# hosts-file editing.
# "*" in development only: a phone testing over Wi-Fi reaches this by whatever
# address the laptop happens to have, and chasing that in settings each time is
# how an afternoon goes.
ALLOWED_HOSTS = ["*"]

# The Vite dev server proxies /api, but direct cross-origin calls are allowed
# locally so the SPA can also be run against a different port.
CORS_ALLOW_ALL_ORIGINS = True

# --- Local substitutes for the managed services (§12.0, T1.24) -------------

# Email to the terminal instead of SES.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = "yardflow@localhost"

# Files to backend/media/ instead of S3. Reads still go through a signed,
# expiring URL so the API contract matches production exactly (T1.16).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# SMS to the log instead of UjumbeSMS (T1.24; the real adapter is T8.11).
#
# A *default*, not a lock. §12.0's guarantee — that no message reaches a real
# technician from a development machine — holds because nobody has to do
# anything to get it. But verifying a new API key is a normal thing to need, and
# a setting that could not be overridden would mean editing this file to do it,
# which is how a hardcoded provider ends up committed. Setting SMS_BACKEND in
# `.env` is a deliberate act, and `manage.py sms_selftest` says which adapter is
# live before it does anything.
SMS_BACKEND = env("SMS_BACKEND", default="notifications.channels.logging_sms.LoggingSmsBackend")

# Run Celery work in-process so no worker is needed for local development.
# Set CELERY_TASK_ALWAYS_EAGER=False once a worker is running.
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=True)
CELERY_TASK_EAGER_PROPAGATES = True

# Verbose SQL is available on demand without editing this file.
if env.bool("DJANGO_LOG_SQL", default=False):
    LOGGING = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "loggers": {"django.db.backends": {"handlers": ["console"], "level": "DEBUG"}},
    }

MEDIA_ROOT = BASE_DIR / "media"

# Links in invitations and notifications point at the Vite dev server, not at
# Django: the app is on 5173, the API on 8000.
APP_URL_TEMPLATE = env("APP_URL_TEMPLATE", default="http://{host}:5173")

# Which tenant a request with no subdomain belongs to — a bare IP address, which
# is how a phone on the same Wi-Fi reaches this machine. Development only; the
# middleware ignores it unless DEBUG.
DEV_TENANT_SLUG = env("DEV_TENANT_SLUG", default="")
