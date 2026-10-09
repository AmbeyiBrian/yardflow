"""Test factories for the site register (design §14)."""

from decimal import Decimal

import factory
from factory.django import DjangoModelFactory

from network.models import Client, Project, Site, SiteReference


class ClientFactory(DjangoModelFactory):
    class Meta:
        model = Client

    name = factory.Sequence(lambda n: f"Operator {n}")


class SiteFactory(DjangoModelFactory):
    class Meta:
        model = Site

    client = factory.SubFactory(ClientFactory)
    internal_ref = factory.Sequence(lambda n: f"SLV-{1000 + n}")
    name = factory.Sequence(lambda n: f"Site {n}")
    # R13: a site can be clocked at only with coordinates (Nairobi CBD).
    latitude = Decimal("-1.292100")
    longitude = Decimal("36.821900")


class SiteReferenceFactory(DjangoModelFactory):
    class Meta:
        model = SiteReference

    site = factory.SubFactory(SiteFactory)
    label = factory.Sequence(lambda n: f"Label {n}")
    value = factory.Sequence(lambda n: f"REF{n}")


class ProjectFactory(DjangoModelFactory):
    class Meta:
        model = Project

    client = factory.SubFactory(ClientFactory)
    reference = factory.Sequence(lambda n: f"WO-{2000 + n}")
