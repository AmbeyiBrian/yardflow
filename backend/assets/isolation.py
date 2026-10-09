"""Isolation fixtures for the asset register (T17.2, T17.8, A3).

``AssetHandover`` has no endpoint of its own: it is read through
``/assets/{id}/handovers``, and the tables are covered at the database by RLS,
which ``assets/tests/test_models.py`` exercises with raw SQL.
"""

from core.isolation import register_isolation_fixture


def register() -> None:
    from assets.models import Asset, AssetType

    def make_asset(organization):
        return Asset.objects.create(
            organization=organization,
            type=AssetType.VEHICLE,
            name="Isolation truck",
            tag="ISO 001A",
        )

    register_isolation_fixture("asset", make_asset, payload={"name": "Renamed"})
