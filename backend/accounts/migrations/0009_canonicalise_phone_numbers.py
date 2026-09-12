"""Put existing phone numbers into one shape (B1).

``normalise_phone`` only stripped punctuation, so ``0722123456`` and
``+254722123456`` — the same phone — were stored as two different strings. The
per-tenant uniqueness constraint therefore missed the duplicate, and somebody
invited under one form could not sign in with the other.

Rows are converted one at a time rather than in a single UPDATE because two of
them may collapse onto the same number, which the unique constraint will refuse.
That is not a failure to recover from: it means the tenant really does hold the
same phone twice, and the migration leaves the later row untouched and says so,
for an administrator to resolve. Refusing to migrate at all would be worse.
"""

import logging
import re

from django.db import migrations

logger = logging.getLogger(__name__)

COUNTRY_CODE = "254"


def canonical(phone: str) -> str | None:
    cleaned = re.sub(r"[^\d+]", "", (phone or "").strip())
    if not cleaned:
        return None
    cleaned = cleaned[0] + cleaned[1:].replace("+", "")
    if cleaned.startswith("+"):
        return cleaned
    if cleaned.startswith("00"):
        return "+" + cleaned[2:]
    if cleaned.startswith(COUNTRY_CODE):
        return "+" + cleaned
    if cleaned.startswith("0"):
        return "+" + COUNTRY_CODE + cleaned[1:]
    return cleaned


def forwards(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    from django.db import IntegrityError, transaction

    for user in User.objects.exclude(phone="").exclude(phone=None).iterator():
        original = user.phone
        wanted = canonical(original)
        if not wanted or wanted == original:
            continue
        try:
            with transaction.atomic():
                user.phone = wanted
                user.save(update_fields=["phone"])
        except IntegrityError:
            # `original`, not `user.phone` — the failed assignment is still on
            # the instance, and reporting it would say the row was converted.
            user.phone = original
            logger.warning(
                "User %s could not be converted to %s: that number already "
                "exists in this organization. Left as %s for an administrator "
                "to resolve.",
                user.pk,
                wanted,
                original,
            )


def backwards(apps, schema_editor):
    """Irreversible in substance — the original spelling is not recorded — but
    declared so the migration can be unapplied without blocking a rollback."""


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_webauthn_rls")]

    operations = [migrations.RunPython(forwards, backwards)]
