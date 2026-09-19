"""A client and a few sites for the **demo** tenant (§12.0, C5, C6).

Called by ``manage.py seed_demo``, not registered as a tenant seeder. A real
customer's site register is theirs to build, and fabricating "Kileleshwa" inside
every organization that gets provisioned would put demo data in production and
break every test that counts sites — which is exactly what happened when this
was wired the other way.

Found while running the end-to-end suite against a freshly seeded tenant: the
gate-out flow picks a site from a list, and with no sites the list was empty and
the spec timed out looking for an option. Those specs had only ever passed
against a tenant somebody had filled in by hand.
"""

from __future__ import annotations

#: Enough to exercise the flows without pretending to be a real register.
DEMO_SITES: tuple[tuple[str, str, str], ...] = (
    ("SLV-1001", "Kileleshwa", "Nairobi"),
    ("SLV-1002", "Karen", "Nairobi"),
    ("SLV-1003", "Nakuru Town", "Nakuru"),
)


def seed_demo_network(organization) -> dict:
    """Create one client and a handful of sites, idempotently."""
    from network.models import Client, Site

    client, _ = Client.objects.get_or_create(
        organization=organization,
        name="Demo Operator",
        defaults={"code": "DEMO"},
    )

    created = 0
    for internal_ref, name, region in DEMO_SITES:
        _, was_created = Site.objects.get_or_create(
            organization=organization,
            internal_ref=internal_ref,
            defaults={"client": client, "name": name, "region": region},
        )
        created += int(was_created)

    return {"client": client, "sites_created": created}
