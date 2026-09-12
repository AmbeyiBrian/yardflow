"""Signal handlers keeping cross-model invariants true (design §4.1)."""

from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Organization, OrganizationSettings


@receiver(post_save, sender=Organization, dispatch_uid="core.create_organization_settings")
def create_organization_settings(sender, instance, created, **kwargs):
    """Give every organization its settings row.

    §4.1 specifies the two are "one-to-one, created together". Doing it here
    rather than in the provisioning service means the invariant also holds for
    organizations created by tests, fixtures, the Django admin or a data
    migration — anywhere at all.
    """
    if created:
        OrganizationSettings.objects.get_or_create(organization=instance)


# --------------------------------------------------------------------------
# B6: every login, failed login and permission change is recorded
# --------------------------------------------------------------------------


@receiver(user_logged_in, dispatch_uid="core.audit_login")
def audit_login(sender, request, user, **kwargs):
    """Record a successful login (B6)."""
    from core.audit import record
    from core.models import AuditAction, AuthMethod

    record(
        AuditAction.LOGIN_SUCCEEDED,
        actor=user,
        organization=getattr(user, "organization_id", None),
        request=request,
        auth_method=AuthMethod.PASSWORD,
    )


@receiver(user_login_failed, dispatch_uid="core.audit_login_failed")
def audit_login_failed(sender, credentials, request=None, **kwargs):
    """Record a failed login (B6).

    The attempted identifier is recorded, never the password — see
    ``core.audit.REDACTED_FIELDS``. An attempt that cannot be attributed to an
    organization goes to the application log instead of a tenant's trail.
    """
    from core.audit import record
    from core.models import AuditAction

    identifier = (
        credentials.get("username") or credentials.get("email") or credentials.get("phone") or ""
    )

    record(
        AuditAction.LOGIN_FAILED,
        actor=None,
        actor_identifier=str(identifier),
        request=request,
        note="Login failed.",
    )


@receiver(user_logged_out, dispatch_uid="core.audit_logout")
def audit_logout(sender, request, user, **kwargs):
    from core.audit import record
    from core.models import AuditAction

    if user is None:
        return
    record(
        AuditAction.LOGOUT,
        actor=user,
        organization=getattr(user, "organization_id", None),
        request=request,
    )
