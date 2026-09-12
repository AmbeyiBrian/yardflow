"""Test factories for identity models (design §14)."""

from datetime import timedelta

import factory
from django.utils import timezone
from factory.django import DjangoModelFactory

from accounts.models import Delegation, Role, RolePermission, User, UserRole


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        # The password hook below saves the instance itself.
        skip_postgeneration_save = True

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    full_name = factory.Sequence(lambda n: f"User {n}")
    is_active = True

    @factory.post_generation
    def password(self, create, extracted, **kwargs):
        """Set a password, or leave the account awaiting an invitation.

        The default is an *unusable* password rather than an empty one. An empty
        string reads as usable to Django, so a factory-made user would look like
        they could log in when they cannot — which would hide exactly the bug
        A1's invitation flow exists to avoid.
        """
        if not create:
            return
        if extracted:
            self.set_password(extracted)
        else:
            self.set_unusable_password()
        self.save(update_fields=["password"])


class RoleFactory(DjangoModelFactory):
    class Meta:
        model = Role
        skip_postgeneration_save = True

    name = factory.Sequence(lambda n: f"Role {n}")

    @factory.post_generation
    def codenames(self, create, extracted, **kwargs):
        """Grant permissions: ``RoleFactory(codenames=[PERM.GATE_OUT_APPROVE])``."""
        if create and extracted:
            self.set_permissions(extracted)


class UserRoleFactory(DjangoModelFactory):
    class Meta:
        model = UserRole

    user = factory.SubFactory(UserFactory)
    role = factory.SubFactory(RoleFactory)


class RolePermissionFactory(DjangoModelFactory):
    class Meta:
        model = RolePermission

    role = factory.SubFactory(RoleFactory)


class DelegationFactory(DjangoModelFactory):
    """An active delegation by default; pass dates to test the window (F5)."""

    class Meta:
        model = Delegation

    from_user = factory.SubFactory(UserFactory)
    to_user = factory.SubFactory(UserFactory)
    starts_at = factory.LazyFunction(lambda: timezone.now() - timedelta(days=1))
    ends_at = factory.LazyFunction(lambda: timezone.now() + timedelta(days=1))
    reason = "Annual leave"
