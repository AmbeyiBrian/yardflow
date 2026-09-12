"""System checks for the notification providers (design §9.3; L1).

There is one way to configure SMS wrongly that gives no sign of being wrong: set
`SMS_BACKEND` to the real provider and leave a credential empty. Nothing fails at
startup, the screens all work, and the first anybody hears of it is a technician
saying they were never told about the approval.

That is not hypothetical. It happened here: the credentials were written into
`.env` as ``KEY = value``, and django-environ's parser requires ``KEY=value`` with
no space before the equals sign — so it skipped all three lines silently. The
adapter was configured, the account was funded, and every message would have
failed with "not configured" until somebody thought to look.

So: if the production adapter is selected, say at startup whether it can actually
send. A warning rather than an error, because `manage.py migrate` on a fresh
machine must not be blocked by an SMS credential.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning, register
from django.utils.module_loading import import_string


@register()
def check_sms_provider_is_configured(app_configs, **kwargs):  # type: ignore[no-untyped-def]
    """Warn when the real SMS provider is selected but cannot send."""
    backend_path = getattr(settings, "SMS_BACKEND", "") or ""

    # Only the provider adapters make a claim worth checking. The logging
    # adapter is the development default and is *meant* to send nothing.
    if "ujumbe" not in backend_path.lower():
        return []

    # Resolve the path that is *configured*, not the one that ought to be. An
    # earlier version of this check imported the correct module regardless of the
    # setting, which meant it could never see the very typo it claims to catch.
    try:
        backend = import_string(backend_path)
    except ImportError as exc:
        # `ujumbe_sws` for `ujumbe_sms` is the one that has actually happened.
        # Otherwise it surfaces only on the first send.
        return [
            Warning(
                f"SMS_BACKEND is set to {backend_path!r}, which cannot be "
                f"imported: {exc}",
                hint=(
                    "The provider adapter is "
                    "notifications.channels.ujumbe_sms.UjumbeSmsBackend."
                ),
                id="notifications.W002",
            )
        ]

    missing = backend.missing_configuration()
    if not missing:
        return []

    return [
        Warning(
            "SMS is pointed at UjumbeSMS but "
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} empty, "
            "so every message will fail.",
            hint=(
                "Set them in backend/.env — with no space before the equals "
                "sign, or the line is ignored. Then check the account with "
                "`manage.py sms_selftest`."
            ),
            id="notifications.W001",
        )
    ]
