"""Test factories for the site register (design §14)."""

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
