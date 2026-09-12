"""Node creation for sites and clients (design §3.1)."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from network.models import Client, Site


@receiver(post_save, sender=Site, dispatch_uid="network.site_node")
def create_site_node(sender, instance, created, **kwargs):
    """H2: installed material is recorded against the site it went into."""
    if created:
        from locations.nodes import node_for_site

        node_for_site(instance)


@receiver(post_save, sender=Client, dispatch_uid="network.client_node")
def create_client_nodes(sender, instance, created, **kwargs):
    """K1, K3: a client is both a destination for returns and a source of issues.

    Two nodes, because they are different places: material *returned to* the
    client sits in transit and remains our exposure until acknowledged (K3),
    while material *issued by* them is where consignment stock comes from.
    """
    if created:
        from locations.nodes import external_node, node_for_client

        node_for_client(instance)
        external_node(instance.organization_id, client=instance)
