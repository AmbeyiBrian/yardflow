"""The starter telecom catalogue (design §4.3; requirements C3, D10).

C3: "the catalogue ships seeded with common telecom items (antennas, RRUs, BBUs,
feeder cable, jumpers, connectors, batteries, rectifiers, tools, PPE) which the
tenant may rename or delete."

Two things this seed is *not*:

* **It is not a schema.** Every row is ordinary tenant data. A tenant may rename
  or delete any of it, and the second customer will want a different list (D10).
* **It is not a source of truth for criticality.** The values below are a
  defensible starting point — active radio equipment and batteries are the
  expensive, stealable, client-owned things; PPE is not — but each tenant tunes
  them, because criticality drives who has to approve a gate-out (C1, F3).
"""

from __future__ import annotations

from catalogue.models import Criticality, ItemCategory, ItemType, TrackingMode

# (name, criticality, [(item name, tracking mode, uom, returnable, return days)])
STARTER_CATALOGUE: list[tuple[str, str, list[tuple[str, str, str, bool, int | None]]]] = [
    (
        "Active equipment",
        # Expensive, serialised, and usually the operator's property. A unit
        # leaving the yard unapproved is the loss this system exists to prevent.
        Criticality.HIGH,
        [
            ("RRU 2x40W", TrackingMode.SERIALIZED, "ea", False, None),
            ("RRU 4x40W", TrackingMode.SERIALIZED, "ea", False, None),
            ("BBU", TrackingMode.SERIALIZED, "ea", False, None),
            ("Baseband board", TrackingMode.SERIALIZED, "ea", False, None),
            ("Microwave ODU", TrackingMode.SERIALIZED, "ea", False, None),
            ("Microwave IDU", TrackingMode.SERIALIZED, "ea", False, None),
        ],
    ),
    (
        "Antennas",
        Criticality.HIGH,
        [
            ("Sector antenna 1800MHz", TrackingMode.SERIALIZED, "ea", False, None),
            ("Sector antenna multiband", TrackingMode.SERIALIZED, "ea", False, None),
            ("Microwave dish 0.3m", TrackingMode.SERIALIZED, "ea", False, None),
            ("Microwave dish 0.6m", TrackingMode.SERIALIZED, "ea", False, None),
        ],
    ),
    (
        "Power",
        # Batteries are the most-stolen item on a telecom site.
        Criticality.HIGH,
        [
            ("Rectifier module", TrackingMode.SERIALIZED, "ea", False, None),
            ("Rectifier cabinet", TrackingMode.SERIALIZED, "ea", False, None),
            ("Battery bank 48V 100Ah", TrackingMode.SERIALIZED, "ea", False, None),
            ("Battery bank 48V 200Ah", TrackingMode.SERIALIZED, "ea", False, None),
            ("DC distribution box", TrackingMode.BULK, "ea", False, None),
        ],
    ),
    (
        "Cable and feeder",
        # D12: reel tracking is required for cable, so these default to REEL and
        # are measured in metres rather than counted.
        Criticality.MEDIUM,
        [
            ("LDF4 feeder cable 1/2 inch", TrackingMode.REEL, "m", False, None),
            ("LDF5 feeder cable 7/8 inch", TrackingMode.REEL, "m", False, None),
            ("Fibre optic cable", TrackingMode.REEL, "m", False, None),
            ("Power cable 16mm", TrackingMode.REEL, "m", False, None),
            ("Earthing cable 35mm", TrackingMode.REEL, "m", False, None),
        ],
    ),
    (
        "Jumpers and connectors",
        Criticality.LOW,
        [
            ("Jumper 1/2 inch 3m", TrackingMode.BULK, "ea", False, None),
            ("Jumper 1/4 inch 1m", TrackingMode.BULK, "ea", False, None),
            ("DIN connector", TrackingMode.BULK, "ea", False, None),
            ("N-type connector", TrackingMode.BULK, "ea", False, None),
            ("Weatherproofing kit", TrackingMode.BULK, "ea", False, None),
            ("Cable clamp", TrackingMode.BULK, "ea", False, None),
        ],
    ),
    (
        "Installation materials",
        Criticality.NONE,
        [
            ("Mounting pole", TrackingMode.BULK, "ea", False, None),
            ("Mounting clamp set", TrackingMode.BULK, "set", False, None),
            ("Cable tie 300mm", TrackingMode.BULK, "pkt", False, None),
            ("Cable ladder 3m", TrackingMode.BULK, "ea", False, None),
            ("Insulation tape", TrackingMode.BULK, "roll", False, None),
        ],
    ),
    (
        "Tools",
        # Returnable: issued to a technician and expected back (I1, I2).
        Criticality.MEDIUM,
        [
            ("Torque wrench", TrackingMode.SERIALIZED, "ea", True, 7),
            ("Cable crimping tool", TrackingMode.SERIALIZED, "ea", True, 7),
            ("Fibre splicing machine", TrackingMode.SERIALIZED, "ea", True, 3),
            ("Power drill", TrackingMode.SERIALIZED, "ea", True, 7),
            ("Multimeter", TrackingMode.SERIALIZED, "ea", True, 14),
            ("Site alignment GPS", TrackingMode.SERIALIZED, "ea", True, 3),
            ("Safety harness", TrackingMode.SERIALIZED, "ea", True, 30),
            ("Ladder 6m", TrackingMode.SERIALIZED, "ea", True, 14),
        ],
    ),
    (
        "PPE",
        # Consumable and cheap. Requiring an owner's approval for a pair of
        # gloves would train people to route around the approval flow entirely.
        Criticality.NONE,
        [
            ("Safety helmet", TrackingMode.BULK, "ea", False, None),
            ("Safety boots", TrackingMode.BULK, "pair", False, None),
            ("High-visibility vest", TrackingMode.BULK, "ea", False, None),
            ("Work gloves", TrackingMode.BULK, "pair", False, None),
            ("Safety goggles", TrackingMode.BULK, "ea", False, None),
        ],
    ),
]


def seed_starter_catalogue(organization) -> dict[str, int]:
    """Create the starter catalogue for a newly provisioned tenant (T2.4).

    Idempotent: called again, it adds only what is missing. Provisioning is
    atomic, but a tenant created before this seeder existed should be able to
    catch up without duplicating everything.
    """
    categories_created = 0
    items_created = 0

    for category_name, criticality, items in STARTER_CATALOGUE:
        category = ItemCategory.objects.filter(
            organization=organization, name=category_name, parent__isnull=True
        ).first()
        if category is None:
            category = ItemCategory.objects.create(
                organization=organization,
                name=category_name,
                criticality=criticality,
            )
            categories_created += 1

        for name, tracking_mode, uom, is_returnable, return_days in items:
            if ItemType.objects.filter(organization=organization, name=name).exists():
                continue
            ItemType.objects.create(
                organization=organization,
                category=category,
                name=name,
                default_tracking_mode=tracking_mode,
                uom=uom,
                is_returnable=is_returnable,
                default_return_days=return_days,
            )
            items_created += 1

    return {"categories": categories_created, "item_types": items_created}
