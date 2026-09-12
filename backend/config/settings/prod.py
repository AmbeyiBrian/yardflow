"""Production settings.

Deferred to Phase 9 (design §12.0, §12.1) — nothing in phases 1–8 depends on
this module. It exists now so that the environment contract is explicit from
the start and so nothing can quietly ship with a development default.

Requirement N-4: every secret comes from the environment (AWS Secrets Manager),
never from the repository. This module **refuses to boot** when a required
variable is absent, rather than falling back to something insecure.
"""

from django.core.exceptions import ImproperlyConfigured

from config.settings.base import *
from config.settings.base import env

DEBUG = False


def _required(name: str) -> str:
    """Return an environment variable, or refuse to start.

    A missing secret must be a loud failure at boot, not a silent fallback that
    reaches production (N-4).
    """
    try:
        value = env(name)
    except Exception as exc:  # environ raises ImproperlyConfigured subclasses
        raise ImproperlyConfigured(
            f"{name} must be set in production. It is read from the environment "
            f"(AWS Secrets Manager); it has no default and never appears in the "
            f"repository."
        ) from exc
    if not value:
        raise ImproperlyConfigured(f"{name} is set but empty; production requires a real value.")
    return value


# --- Secrets and hosts ----------------------------------------------------

SECRET_KEY = _required("DJANGO_SECRET_KEY")

# Wildcard host for tenant subdomains, e.g. ".yardflow.co.ke" (§12.1).
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must list the tenant wildcard domain.")

# The ALB health check probes by IP or internal DNS name. Adding it here keeps
# probes from generating DisallowedHost noise (§12.1).
_health_check_host = env("HEALTH_CHECK_HOST", default="")
if _health_check_host:
    ALLOWED_HOSTS.append(_health_check_host)

DATABASES = {"default": env.db_url("DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
# Reuse connections across requests. Safe because `SET LOCAL app.current_org`
# is transaction-scoped and can never leak between requests (§2.2).
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)

CELERY_BROKER_URL = _required("REDIS_URL")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL


# --- TLS and cookies (N-4: all traffic over TLS) --------------------------

SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
X_FRAME_OPTIONS = "DENY"

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])


# --- Attachments: S3, private, pre-signed only (N-7) ----------------------

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": _required("AWS_STORAGE_BUCKET_NAME"),
            "region_name": env("AWS_S3_REGION_NAME", default="eu-west-1"),
            # N-7: never publicly readable; every read is a short-lived
            # pre-signed GET.
            "default_acl": "private",
            "querystring_auth": True,
            "querystring_expire": env.int("AWS_S3_URL_EXPIRY_SECONDS", default=300),
            "file_overwrite": False,
            "signature_version": "s3v4",
        },
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    *MIDDLEWARE[1:],
]


# --- Email and SMS --------------------------------------------------------

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"  # SES SMTP interface
EMAIL_HOST = env("EMAIL_HOST", default="email-smtp.eu-west-1.amazonaws.com")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_USE_TLS = True
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@yardflow.co.ke")

# Real SMS delivery (T8.11). Credentials from the environment, never the repo
# (N-4). All three are needed: UjumbeSMS authenticates with the API key *and* the
# account email, and refuses a sender ID that is not approved for the account.
SMS_BACKEND = "notifications.channels.ujumbe_sms.UjumbeSmsBackend"
UJUMBE_SMS_API_KEY = env("UJUMBE_SMS_API_KEY", default="")
UJUMBE_SMS_ACCOUNT_EMAIL = env("UJUMBE_SMS_ACCOUNT_EMAIL", default="")
UJUMBE_SMS_SENDER_ID = env("UJUMBE_SMS_SENDER_ID", default="")
# UjumbeSMS publishes two hosts and an account is issued against one of them.
# Overridable so switching is an env change (T8.11's "switching provider is a
# settings change" applies to the host as much as to the adapter).
UJUMBE_SMS_BASE_URL = env("UJUMBE_SMS_BASE_URL", default="https://ujumbesms.co.ke")

# WhatsApp (T8.12, Q1). The adapter is written and unit-tested; it stays **off**
# until Meta approves a sender and message templates, which is a business
# process rather than a code change. Supplying these two and turning the channel
# on in the tenant's notification settings is all that is left.
WHATSAPP_ACCESS_TOKEN = env("WHATSAPP_ACCESS_TOKEN", default="")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID", default="")


# --- Structured logging and error tracking (N-11) -------------------------
# The base configuration already attaches the organization and request id to
# every record; production only swaps the formatter for JSON so CloudWatch can
# index the fields (§12.1).

LOGGING["handlers"]["console"]["formatter"] = "json"  # type: ignore[index]
LOGGING["root"]["level"] = env("DJANGO_LOG_LEVEL", default="INFO")  # type: ignore[index]
