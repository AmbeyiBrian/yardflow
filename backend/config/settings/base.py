"""Settings shared by every environment.

Design §1.1, §12. Requirement N-4: no secret is ever committed. Everything
environment-specific arrives through environment variables, which come from a
git-ignored ``.env`` locally and from AWS Secrets Manager in production.
"""

from pathlib import Path

import environ

# backend/config/settings/base.py -> backend/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

# Read backend/.env when present. Absent in production, where the platform
# supplies real environment variables instead.
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    env.read_env(str(_env_file))


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

# Overridden in dev with an insecure default; prod requires it explicitly.
SECRET_KEY = env("DJANGO_SECRET_KEY", default=None)

DEBUG = False

ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
ROOT_URLCONF = "config.urls"

# Tenants are addressed by subdomain (§2.2), so the trailing-slash redirect and
# host handling must never rewrite the host.
APPEND_SLASH = True


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",  # revocable refresh tokens (B1)
    "django_filters",
    "corsheaders",
    "drf_spectacular",
    "storages",
]

# Design §1.2. Order is dependency-friendly: core first, platform_admin last.
LOCAL_APPS = [
    "core",
    "accounts",
    "catalogue",
    "network",
    "locations",
    "stock",
    "receiving",
    "dispatch",
    "approvals",
    "jobs",
    "custody",
    "disposition",
    "notifications",
    "commercials",
    "reporting",
    "sync",
    "platform_admin",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    # First, so every log line from the request carries its id (N-11).
    "core.logging.RequestIdMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Before anything that touches the database or the tenant: a request that is
    # only going to be redirected should not open a transaction, and a stale
    # client asking for `/api/v1/gate-ins/` deserves the record rather than an
    # HTML 404 it cannot read (§6.1).
    "core.api_urls.ApiUrlCanonicalisationMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Last, and after authentication: it falls back to the authenticated user's
    # organization when no subdomain addressed one (§2.2).
    "core.middleware.TenantMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
# Postgres only. Row-level security (§2.3), JSONB and window functions for the
# as-of-date ledger (§3.4) are not optional parts of the design.

DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default="postgres://yardflow:yardflow@localhost:5432/yardflow",
    ),
}

# §2.2: TenantMiddleware issues `SET LOCAL app.current_org`, which is
# transaction-scoped. ATOMIC_REQUESTS guarantees a transaction exists, so a
# pooled connection can never leak the setting into the next request.
DATABASES["default"]["ATOMIC_REQUESTS"] = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

#: Expands a national phone number to international form (``0722…`` becomes
#: ``+254722…``), so one person's number is one string. Kenya by default, since
#: that is where the yards are; set it empty to store numbers exactly as typed.
DEFAULT_COUNTRY_CALLING_CODE = env("DEFAULT_COUNTRY_CALLING_CODE", default="254")

# B1: log in with an email address or a phone number, resolved within the
# tenant. ModelBackend stays for the Django admin site's own login.
AUTHENTICATION_BACKENDS = [
    "accounts.authentication_backends.EmailOrPhoneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------
# N-8: English only in v1, strings externalised so localisation stays possible.
# N-9: KES and Africa/Nairobi are the platform defaults; each tenant may
# override both in OrganizationSettings (§4.1).

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", default="Africa/Nairobi")
USE_I18N = True
USE_TZ = True

DEFAULT_CURRENCY = "KES"


# --------------------------------------------------------------------------
# Static and media
# --------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"


# --------------------------------------------------------------------------
# DRF
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # Reconciles the caller with the tenant the subdomain addresses (§2.2).
        "core.authentication.TenantJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        # A2: a suspended tenant may read but not write. Applied by default so
        # a new endpoint cannot forget it.
        "core.api_permissions.OrganizationIsActive",
    ],
    # §6: cursor pagination on list endpoints, so deep pages stay cheap.
    "DEFAULT_PAGINATION_CLASS": "core.pagination.TimestampCursorPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # The §6.1 error envelope. Installed by T1.8.
    "EXCEPTION_HANDLER": "core.api.exception_handler",
    "ORDERING_PARAM": "ordering",
}

SIMPLE_JWT = {
    "ROTATE_REFRESH_TOKENS": True,
    # B1: refresh tokens are revocable, so logout genuinely ends a session.
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "YardFlow API",
    "DESCRIPTION": "Yard inventory and gate control.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    # The same choice set appears as `default_tracking_mode` on an item type and
    # as `tracking_mode` on a document line, so the generator cannot pick a name
    # on its own. Naming it once keeps a generated client from producing two
    # identical enums (N-10).
    "ENUM_NAME_OVERRIDES": {
        "TrackingModeEnum": "catalogue.models.TrackingMode.choices",
        "ConditionEnum": "stock.models.Condition.choices",
        "OwnerTypeEnum": "stock.models.OwnerType.choices",
        "CriticalityEnum": "catalogue.models.Criticality.choices",
    },
}


# --------------------------------------------------------------------------
# Celery
# --------------------------------------------------------------------------

from celery.schedules import crontab  # noqa: E402  (settings are read top-down)

CELERY_BROKER_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
# Notifications must never be lost, but must never block a business
# transaction either (L3, §9.1) — they are dispatched on_commit.
CELERY_TASK_ACKS_LATE = True

# The clock-driven half of the product (§12, I3, F5, K3, Q3). Several
# requirements are only met by something running on a schedule: an overdue tool
# nobody is reminded about is a lost tool, and an approved gate pass that never
# expires is a stale authorisation.
#
# Early morning, before the yard opens: a storekeeper arriving at seven should
# find last night's overdue flags and expiries already applied, not watch them
# appear mid-shift.
CELERY_BEAT_SCHEDULE = {
    "sweeps": {
        "task": "core.sweeps.dispatch_sweeps",
        "schedule": crontab(hour="5", minute="30"),
    },
    # L3: deliveries that failed transiently are retried rather than lost. More
    # often than the sweeps, because a notification that arrives an hour late has
    # already missed the decision it was about.
    "retry-notifications": {
        "task": "core.sweeps.retry_notifications",
        "schedule": crontab(minute="*/15"),
    },
    # §3.4: the ledger is the truth and the balances are a cache. This reports
    # drift; it never corrects it, because a cache that silently self-heals hides
    # the bug that caused the drift.
    "verify-ledger": {
        "task": "core.sweeps.verify_ledgers",
        "schedule": crontab(hour="2", minute="0"),
    },
}


# --------------------------------------------------------------------------
# Notification providers (§9; L1, T8.11, T8.12)
# --------------------------------------------------------------------------
# Declared here with empty defaults so every environment reads the same names,
# and so `manage.py sms_selftest` can say which one is missing rather than
# failing with an AttributeError. Values come from the environment only (N-4).
#
# Development keeps the logging adapter (§12.0): nothing can reach a real
# technician's phone from a developer's machine, whatever is in `.env`.
SMS_BACKEND = env(
    "SMS_BACKEND", default="notifications.channels.logging_sms.LoggingSmsBackend"
)
UJUMBE_SMS_API_KEY = env("UJUMBE_SMS_API_KEY", default="")
UJUMBE_SMS_ACCOUNT_EMAIL = env("UJUMBE_SMS_ACCOUNT_EMAIL", default="")
UJUMBE_SMS_SENDER_ID = env("UJUMBE_SMS_SENDER_ID", default="")
UJUMBE_SMS_BASE_URL = env("UJUMBE_SMS_BASE_URL", default="https://ujumbesms.co.ke")

# Q1: written, tested, and off. Enabling it is these two values plus the channel
# switch in the tenant's notification settings — no code change, no deployment.
WHATSAPP_ACCESS_TOKEN = env("WHATSAPP_ACCESS_TOKEN", default="")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID", default="")


# --------------------------------------------------------------------------
# Domain defaults
# --------------------------------------------------------------------------

# The subdomain that addresses the cross-tenant platform admin console (§2.2),
# and the base domain tenant subdomains hang off (A1).
PLATFORM_ADMIN_SUBDOMAIN = env("PLATFORM_ADMIN_SUBDOMAIN", default="admin")
TENANT_BASE_DOMAIN = env("TENANT_BASE_DOMAIN", default="localhost")

# The domain fingerprint approvals are scoped to (B5, D9). Empty means "use
# TENANT_BASE_DOMAIN", which is right in production: `yardflow.co.ke` covers
# every tenant subdomain hanging off it, and a credential survives a tenant being
# addressed differently.
#
# It has to be settable because the rule browsers apply is strict — the id must
# equal the domain in the address bar or be a *registrable parent* of it — and
# `localhost` is not a registrable parent of `demo.localhost`; browsers treat it
# as a top-level name. So local development needs the exact host, and without
# this line the setting could be put in `.env` and have no effect at all, which
# is how it was found.
# Where the *app* lives, for links people follow out of an email or an SMS —
# password invitations and approval deep links. `{host}` is filled with the
# tenant's own subdomain, which is what resolves their organization (§2.2).
#
# It needs its own setting because the app is not always on 443 next to the API:
# in development it is Vite on 5173 while Django is on 8000, so a link built from
# the request would send a new owner to a page that does not exist.
APP_URL_TEMPLATE = env("APP_URL_TEMPLATE", default="https://{host}")

WEBAUTHN_RP_ID = env("WEBAUTHN_RP_ID", default="")
WEBAUTHN_RP_NAME = env("WEBAUTHN_RP_NAME", default="YardFlow")


# --------------------------------------------------------------------------
# Silenced system checks
# --------------------------------------------------------------------------

SILENCED_SYSTEM_CHECKS = [
    # auth.E003: USERNAME_FIELD must be unique.
    #
    # Deliberate. Requirement B1 makes email and phone unique *per tenant*, not
    # globally, so that two customer companies can each have a user with the
    # same address. Uniqueness is enforced by partial unique constraints on
    # accounts.User (including ones covering platform admins, whose organization
    # is NULL). Login resolves the identifier within a tenant, so a non-unique
    # USERNAME_FIELD is correct here rather than an oversight.
    "auth.E003",
    # auth.W004: the same situation, reported as a warning once a custom
    # authentication backend is present. `EmailOrPhoneBackend` handles the
    # non-unique identifier deliberately, by scoping the lookup to one
    # organization — which is exactly what the hint asks for.
    "auth.W004",
]


# --------------------------------------------------------------------------
# Logging (N-11)
# --------------------------------------------------------------------------
# Every record carries the request id and the organization it belongs to. When a
# customer reports a problem, the first question is which customer — a shared
# log without that field is guesswork.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "tenant_context": {"()": "core.logging.TenantContextFilter"},
    },
    "formatters": {
        "plain": {
            "format": (
                "{levelname} {asctime} {name} "
                "org={organization_id} req={request_id} {message}"
            ),
            "style": "{",
        },
        "json": {
            "()": "pythonjsonlogger.json.JsonFormatter",
            "format": (
                "%(asctime)s %(levelname)s %(name)s %(message)s "
                "%(organization_id)s %(request_id)s"
            ),
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
            "filters": ["tenant_context"],
        },
    },
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}

# Error tracking is optional and off unless a DSN is supplied (N-11).
SENTRY_DSN = env("SENTRY_DSN", default="")
SENTRY_TRACES_SAMPLE_RATE = env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0)
ENVIRONMENT = env("ENVIRONMENT", default="local")

# Largest attachment accepted (D6, G3).
ATTACHMENT_MAX_BYTES = env.int("ATTACHMENT_MAX_BYTES", default=25 * 1024 * 1024)
