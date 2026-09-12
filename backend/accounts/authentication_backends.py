"""Authenticating by email *or* phone, within a tenant (requirement B1).

B1: "log in with either my email address or my phone number plus a password, so
that field staff without email can still use the system."

Because both identifiers are unique only *within* an organization (§4.2), the
lookup must be scoped. Authenticating globally would mean two tenants' users
with the same address could log into each other's accounts — so the tenant is
part of the credential, not an afterthought.
"""

from __future__ import annotations

from django.contrib.auth.backends import BaseBackend
from django.db.models import Q

from accounts.models import User, normalise_phone


class EmailOrPhoneBackend(BaseBackend):
    """Resolve a user from an email address or a phone number."""

    def authenticate(  # type: ignore[override]
        self,
        request,
        identifier: str | None = None,
        password: str | None = None,
        organization_id=None,
        **kwargs,
    ):
        if not identifier or not password:
            return None

        user = find_user_by_identifier(identifier, organization_id=organization_id)
        if user is None:
            # Hash a dummy password anyway, so that a missing user and a wrong
            # password take comparable time and the response does not reveal
            # which identifiers exist.
            User().set_password(password)
            return None

        if not user.check_password(password):
            return None
        if not self.user_can_authenticate(user):
            return None
        return user

    def user_can_authenticate(self, user: User) -> bool:
        """B3: a deactivated user keeps their records but loses access."""
        return bool(user.is_active)

    def get_user(self, user_id):  # type: ignore[no-untyped-def]
        return User.objects.filter(pk=user_id, is_active=True).first()


def find_user_by_identifier(identifier: str, *, organization_id=None) -> User | None:
    """Find a user by email or phone within one organization.

    ``organization_id=None`` means the platform-admin scope: users with no
    organization. It never means "any organization" — that would defeat the
    per-tenant uniqueness the whole model is built on.
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return None

    lookup = Q(email__iexact=identifier)
    phone = normalise_phone(identifier)
    if phone:
        lookup |= Q(phone=phone)

    return (
        User.objects.filter(lookup).filter(organization_id=organization_id).order_by("pk").first()
    )
