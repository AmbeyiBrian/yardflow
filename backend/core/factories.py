"""Test factories for core models (design §14).

Every tenant-scoped factory must be created inside an active tenant context, so
that the ``TenantModel.save()`` guard has an organization to stamp.
"""

import factory
from factory.django import DjangoModelFactory

from core.models import Organization


class OrganizationFactory(DjangoModelFactory):
    """An organization, with its settings row created by signal (§4.1)."""

    class Meta:
        model = Organization
        skip_postgeneration_save = True

    name = factory.Sequence(lambda n: f"Tenant {n}")
    # Unique per platform and immutable once set (A1).
    slug = factory.Sequence(lambda n: f"tenant-{n}")
