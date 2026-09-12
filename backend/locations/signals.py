"""Keeping the node graph complete (design §3.1, §4.5; C4, J1).

Signals rather than explicit calls, so the invariants hold for records created by
the API, the Django admin, a fixture, a data migration or a test — not only by
the code paths someone remembered to update.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from locations.models import Location, LocationType


@receiver(post_save, sender=Location, dispatch_uid="locations.location_node")
def create_location_node(sender, instance, created, **kwargs):
    """Every location is somewhere material can be, so every location is a node."""
    if not created:
        return

    from locations.nodes import ensure_yard_quarantine, node_for_location

    node_for_location(instance)

    # C4/J1: "every yard has a quarantine child". Created with the yard, because
    # a gate-in that receives a faulty line must have somewhere to put it (D2)
    # and cannot stop to create one.
    if instance.type == LocationType.YARD:
        ensure_yard_quarantine(instance)
