"""Password reset and invitation tokens (requirements B2, A1).

Uses Django's :class:`PasswordResetTokenGenerator`, which gives two properties
the requirement needs without inventing anything:

* **single-use** — the token is derived from the password hash, so setting a
  password invalidates every outstanding token for that user
* **expiring** — bounded by ``PASSWORD_RESET_TIMEOUT``

The same mechanism serves the owner invitation in A1: an invited user has an
unusable password and follows a link to set their first one. One code path, and
no temporary password ever exists to be intercepted.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from accounts.models import User
from notifications.channels import RenderedMessage, get_sms_backend

logger = logging.getLogger(__name__)


def encode_uid(user: User) -> str:
    return urlsafe_base64_encode(force_bytes(user.pk))


def decode_uid(uid: str) -> User | None:
    try:
        pk = force_str(urlsafe_base64_decode(uid))
        return User.objects.filter(pk=pk).first()
    except (TypeError, ValueError, OverflowError):
        return None


def make_reset_token(user: User) -> str:
    return default_token_generator.make_token(user)


def check_reset_token(user: User, token: str) -> bool:
    return default_token_generator.check_token(user, token)


def reset_url(user: User, token: str, *, request=None) -> str:
    """Build the link the user follows.

    Addressed at the **tenant's own subdomain**, because that is what resolves
    their organization (§2.2) — a link on the bare domain would land them
    nowhere.

    That host is derived from the account, never from the request. An earlier
    version replaced it with `request.get_host()` whenever the connection was
    not HTTPS, which meant an invitation sent while provisioning a tenant from
    the Django admin pointed the new owner at the *admin* host — in development,
    at the API on `127.0.0.1:8000`, a page that does not exist. Whoever sends
    the invitation is irrelevant to where the recipient has to go.

    `APP_URL_TEMPLATE` decides scheme and port, because the app is not always on
    443: in development it is Vite on 5173 while the API is on 8000.
    """
    base_domain = getattr(settings, "TENANT_BASE_DOMAIN", "localhost")
    organization = user.organization

    if organization is not None:
        host = f"{organization.slug}.{base_domain}"
    else:
        host = f"{settings.PLATFORM_ADMIN_SUBDOMAIN}.{base_domain}"

    template = getattr(settings, "APP_URL_TEMPLATE", "") or "https://{host}"
    origin = template.format(host=host).rstrip("/")

    return f"{origin}/reset-password?uid={encode_uid(user)}&token={token}"


def _invitation_body(user: User, url: str) -> tuple[str, str]:
    where = f" at {user.organization.name}" if user.organization else ""
    subject = "Set your YardFlow password"
    body = f"An account has been created for you{where}.\n\nSet your password here:\n{url}\n"
    return subject, body


def _reset_body(url: str) -> tuple[str, str]:
    subject = "Reset your YardFlow password"
    body = (
        "A password reset was requested for your account.\n\n"
        f"Reset it here:\n{url}\n\n"
        "If you did not request this, you can ignore this message.\n"
    )
    return subject, body


def send_password_reset(user: User, *, request=None, is_invitation: bool = False) -> str:
    """Send a reset or invitation link by whichever channel can reach the user.

    A technician may have a phone number and no email address (B1), so the
    channel follows the identifier rather than assuming email.
    """
    token = make_reset_token(user)
    url = reset_url(user, token, request=request)

    subject, body = _invitation_body(user, url) if is_invitation else _reset_body(url)

    if user.email:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )

    if user.phone and user.organization_id:
        # B2 allows SMS delivery. Behind the channel interface, so the local
        # logging adapter and the real Ujumbe adapter are interchangeable.
        #
        # Metered like every other SMS (L4). This path used to call the provider
        # directly: a real message at real cost, with no credit deducted and no
        # record anywhere — so a tenant's balance said one thing and their bill
        # said another. An invitation is worth sending, but it is not free.
        from notifications import credits

        # If they have no email address, this SMS is the only way they will ever
        # get in — B1 exists precisely so a technician with no company email can
        # be a user. Refusing it for want of one credit would lock a person out
        # of the system over a shilling, so it is allowed to overdraw: the
        # balance goes to -1, an administrator can see exactly why, and they
        # settle it. Every message about *work* still stops at zero.
        unreachable_otherwise = not user.email

        try:
            credits.spend_one(
                user.organization,
                None,
                allow_overdraft=unreachable_otherwise,
                note=(
                    "Sign-in link — no email address, sent on credit"
                    if unreachable_otherwise
                    else "Sign-in link"
                ),
            )
        except credits.NoCredit:
            logger.warning(
                "no SMS credit for organization %s: link sent by email only",
                user.organization_id,
            )
        else:
            get_sms_backend().send(
                user.phone, RenderedMessage(subject=subject, body=f"{subject}: {url}")
            )

    from core.audit import record
    from core.models import AuditAction

    record(
        AuditAction.PASSWORD_RESET_REQUESTED,
        actor=user,
        organization=user.organization_id,
        target=user,
        request=request,
        note="Invitation sent." if is_invitation else "Reset link sent.",
    )

    return token
