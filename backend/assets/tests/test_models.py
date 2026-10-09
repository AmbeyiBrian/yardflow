"""T17.2 — the asset register's tables (§4.20.2; R14)."""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from accounts.factories import UserFactory
from accounts.models import Role, RolePermission
from accounts.permissions_registry import ALL_CODENAMES, DEFAULT_ROLES, PERM
from assets.models import (
    Asset,
    AssetCloseReason,
    AssetHandover,
    AssetStatus,
    AssetType,
)
from commercials.models import ExpenseCategory, ProjectExpense
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from custody.services import HolderStillHasMaterial, assert_can_deactivate
from network.factories import ProjectFactory


def make_asset(tenant, **overrides):
    values = {
        "organization": tenant,
        "type": AssetType.VEHICLE,
        "name": "Hilux",
        "tag": "KDA 123A",
    }
    values.update(overrides)
    return Asset.objects.create(**values)


@pytest.mark.django_db
class TestAsset:
    def test_the_tag_key_ignores_case_spaces_and_dashes(self, tenant):
        asset = make_asset(tenant, tag="kda-123 a")

        assert asset.tag_key == "KDA123A"

    def test_two_assets_cannot_share_a_tag_however_it_is_typed(self, tenant):
        make_asset(tenant, tag="KDA 123A")

        with pytest.raises(IntegrityError), transaction.atomic():
            make_asset(tenant, name="Other", tag="kda-123a")

    def test_the_same_tag_is_fine_in_another_tenant(self, tenant, other_organization):
        make_asset(tenant)
        with tenant_context(other_organization):
            make_asset(other_organization)

        assert Asset.objects.count() == 1

    def test_blank_tags_do_not_collide(self, tenant):
        make_asset(tenant, type=AssetType.TOOL, name="Drill", tag="")
        make_asset(tenant, type=AssetType.TOOL, name="Saw", tag="")

        assert Asset.objects.filter(tag_key="").count() == 2

    def test_a_closed_asset_needs_a_date_and_a_reason(self, tenant):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_asset(tenant, status=AssetStatus.CLOSED)

        closed = make_asset(
            tenant,
            status=AssetStatus.CLOSED,
            closed_on=date(2026, 10, 1),
            closed_reason=AssetCloseReason.SOLD,
        )
        assert closed.pk

    def test_vehicle_fields_are_refused_on_other_types_in_the_database(self, tenant):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_asset(
                tenant, type=AssetType.GENERATOR, tag="", insurance_expires_on=date(2027, 1, 1)
            )

    def test_clean_refuses_vehicle_fields_on_other_types(self, tenant):
        generator = Asset(
            organization=tenant, type=AssetType.GENERATOR, name="Gen", make="Perkins"
        )

        with pytest.raises(ValidationError) as caught:
            generator.clean()

        assert "make" in caught.value.message_dict

    def test_clean_needs_a_registration_on_a_vehicle(self, tenant):
        vehicle = Asset(organization=tenant, type=AssetType.VEHICLE, name="Van", tag=" ")

        with pytest.raises(ValidationError) as caught:
            vehicle.clean()

        assert "tag" in caught.value.message_dict

    def test_an_asset_is_never_deleted(self, tenant):
        asset = make_asset(tenant)

        with pytest.raises(ValidationError):
            asset.delete()

        assert Asset.objects.filter(pk=asset.pk).exists()

    def test_it_is_in_the_yard_until_someone_holds_it(self, tenant):
        assert make_asset(tenant).holder is None


@pytest.mark.django_db
class TestAssetHandover:
    def handover(self, tenant, asset, **overrides):
        values = {
            "organization": tenant,
            "asset": asset,
            "handed_over_by": UserFactory(organization=tenant),
            "handed_over_on": date(2026, 10, 1),
        }
        values.update(overrides)
        return AssetHandover.objects.create(**values)

    def test_the_yard_to_a_person_is_recorded(self, tenant):
        driver = UserFactory(organization=tenant)

        row = self.handover(tenant, make_asset(tenant), to_holder=driver)

        assert row.from_holder is None and row.to_holder == driver

    def test_a_handover_must_change_hands(self, tenant):
        asset = make_asset(tenant)
        driver = UserFactory(organization=tenant)

        with pytest.raises(IntegrityError), transaction.atomic():
            self.handover(tenant, asset, from_holder=driver, to_holder=driver)
        with pytest.raises(IntegrityError), transaction.atomic():
            self.handover(tenant, asset)  # yard to yard

    def test_a_handover_cannot_be_changed_or_removed(self, tenant):
        row = self.handover(
            tenant, make_asset(tenant), to_holder=UserFactory(organization=tenant)
        )

        with pytest.raises(Exception, match="not permitted"), transaction.atomic():
            AssetHandover.objects.filter(pk=row.pk).update(note="edited")
        with pytest.raises(Exception, match="not permitted"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('DELETE FROM "assets_assethandover"')

        assert AssetHandover.objects.filter(pk=row.pk, note="").exists()


@pytest.mark.django_db
class TestVehicleOnExpense:
    def test_a_fuel_expense_can_name_the_vehicle_and_keep_the_typed_reg(self, tenant):
        vehicle = make_asset(tenant)
        category = ExpenseCategory.objects.create(organization=tenant, name="Fuel")
        project = ProjectFactory()
        who = UserFactory(organization=tenant)

        def expense(**kw):
            return ProjectExpense.objects.create(
                organization=tenant,
                project=project,
                category=category,
                amount=Decimal("500.00"),
                incurred_on=date(2026, 10, 1),
                recorded_by=who,
                **kw,
            )

        named = expense(vehicle=vehicle, vehicle_reg="KDA 123A")
        not_ours = expense(vehicle_reg="KZZ 999Z")

        assert named.vehicle == vehicle
        assert not_ours.vehicle is None
        assert list(vehicle.expenses.all()) == [named]

    def test_a_vehicle_with_fuel_against_it_cannot_be_deleted_from_the_database(self, tenant):
        vehicle = make_asset(tenant)
        category = ExpenseCategory.objects.create(organization=tenant, name="Fuel")
        ProjectExpense.objects.create(
            organization=tenant,
            project=ProjectFactory(),
            category=category,
            amount=Decimal("500.00"),
            incurred_on=date(2026, 10, 1),
            recorded_by=UserFactory(organization=tenant),
            vehicle=vehicle,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                # Foreign keys are deferred; check at the statement.
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                cursor.execute('DELETE FROM "assets_asset"')


@pytest.mark.django_db
class TestDeactivatingAHolder:
    def test_someone_holding_an_asset_cannot_be_deactivated(self, tenant):
        driver = UserFactory(organization=tenant)
        make_asset(tenant, holder=driver)

        with pytest.raises(HolderStillHasMaterial) as caught:
            assert_can_deactivate(driver)

        assert [a["tag"] for a in caught.value.details["assets"]] == ["KDA 123A"]

    def test_a_closed_asset_no_longer_blocks(self, tenant):
        driver = UserFactory(organization=tenant)
        make_asset(
            tenant,
            holder=driver,
            status=AssetStatus.CLOSED,
            closed_on=date(2026, 10, 1),
            closed_reason=AssetCloseReason.SOLD,
        )

        assert_can_deactivate(driver)

    def test_someone_holding_nothing_can_be_deactivated(self, tenant):
        assert_can_deactivate(UserFactory(organization=tenant))


@pytest.mark.django_db
class TestPermission:
    def test_the_owner_holds_asset_manage_and_nobody_else_by_default(self):
        assert PERM.ASSET_MANAGE in ALL_CODENAMES
        holders = [n for n, codes in DEFAULT_ROLES.items() if PERM.ASSET_MANAGE in codes]

        assert holders == ["Owner"]

    def test_a_new_tenant_owner_is_seeded_with_it(self):
        result = provision_tenant(name="Acme", slug="acme", owner_email="o@acme.co.ke")

        with tenant_context(result["organization"]):
            owner = Role.objects.get(name="Owner")
            assert RolePermission.objects.filter(
                role=owner, codename="asset.manage"
            ).exists()


@pytest.mark.django_db
@pytest.mark.rls
class TestIsolation:
    def test_raw_sql_sees_only_the_active_tenants_assets(self, organization, other_organization):
        with tenant_context(organization):
            make_asset(organization, name="Ours", tag="A 1")
        with tenant_context(other_organization):
            make_asset(other_organization, name="Theirs", tag="B 1")

        def names():
            with connection.cursor() as cursor:
                cursor.execute('SELECT name FROM "assets_asset"')
                return [r[0] for r in cursor.fetchall()]

        with tenant_context(organization):
            assert names() == ["Ours"]
        with tenant_context(other_organization):
            assert names() == ["Theirs"]

    def test_both_tables_carry_the_policy(self):
        from core.rls import tables_with_policy

        tables = set(tables_with_policy())
        assert {"assets_asset", "assets_assethandover"} <= tables
