"""T2.1–T2.4 — the item catalogue (§4.3; C1, C2, C3, D10, D11, D12)."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from catalogue.factories import (
    CategoryCustomFieldFactory,
    ItemCategoryFactory,
    ItemTypeFactory,
)
from catalogue.models import (
    CategoryCustomField,
    Criticality,
    ItemCategory,
    ItemType,
    TrackingMode,
)
from catalogue.seed import STARTER_CATALOGUE, seed_starter_catalogue


class TestCategoryHierarchy:
    """C1: categories are hierarchical, at least two levels."""

    def test_a_top_level_category_has_depth_one(self, tenant):
        assert ItemCategoryFactory(name="Active equipment").depth == 1

    def test_a_child_category_has_depth_two(self, tenant):
        parent = ItemCategoryFactory(name="Active equipment")

        assert ItemCategoryFactory(name="Radios", parent=parent).depth == 2

    def test_a_third_level_is_refused(self, tenant):
        """The limit is enforced, not merely conventional.

        Routing has to resolve a criticality quickly and predictably (F3); an
        arbitrarily deep tree makes that both slow and hard to reason about.
        """
        parent = ItemCategoryFactory(name="Active equipment")
        child = ItemCategoryFactory(name="Radios", parent=parent)

        with pytest.raises(ValidationError, match="levels deep"):
            ItemCategoryFactory(name="Too deep", parent=child)

    def test_a_category_cannot_be_its_own_parent(self, tenant):
        category = ItemCategoryFactory()
        category.parent = category

        with pytest.raises(ValidationError):
            category.save()

    def test_names_are_unique_within_a_parent(self, tenant):
        parent = ItemCategoryFactory(name="Power")
        ItemCategoryFactory(name="Batteries", parent=parent)

        with pytest.raises(IntegrityError), transaction.atomic():
            ItemCategory.objects.create(
                organization=parent.organization, name="Batteries", parent=parent
            )

    def test_the_same_name_may_appear_under_different_parents(self, tenant):
        first = ItemCategoryFactory(name="Power")
        second = ItemCategoryFactory(name="Transmission")

        ItemCategoryFactory(name="Spares", parent=first)
        ItemCategoryFactory(name="Spares", parent=second)

        assert ItemCategory.objects.filter(name="Spares").count() == 2


class TestCriticality:
    """C1, F3: criticality drives approval routing."""

    def test_criticality_defaults_to_none(self, tenant):
        assert ItemCategoryFactory().criticality == Criticality.NONE

    def test_a_child_inherits_its_parents_criticality(self, tenant):
        """An admin who sets HIGH on a parent expects children to follow.

        Without inheritance, adding a subcategory would quietly drop the
        approval requirement on everything in it (F3).
        """
        parent = ItemCategoryFactory(name="Power", criticality=Criticality.HIGH)
        child = ItemCategoryFactory(name="Batteries", parent=parent)

        assert child.effective_criticality() == Criticality.HIGH

    def test_a_child_may_override_its_parent(self, tenant):
        parent = ItemCategoryFactory(name="Power", criticality=Criticality.HIGH)
        child = ItemCategoryFactory(
            name="Cable ties", parent=parent, criticality=Criticality.LOW
        )

        assert child.effective_criticality() == Criticality.LOW

    def test_an_item_type_reports_its_categorys_criticality(self, tenant):
        category = ItemCategoryFactory(criticality=Criticality.HIGH)

        assert ItemTypeFactory(category=category).criticality == Criticality.HIGH


class TestCustomFields:
    """C2: custom fields per category, with types and required-at-gate-in."""

    def test_a_dropdown_needs_options(self, tenant):
        with pytest.raises(ValidationError, match="at least one option"):
            CategoryCustomFieldFactory(
                field_type=CategoryCustomField.FieldType.DROPDOWN, options=[]
            )

    def test_a_non_dropdown_takes_no_options(self, tenant):
        with pytest.raises(ValidationError, match="takes no options"):
            CategoryCustomFieldFactory(
                field_type=CategoryCustomField.FieldType.TEXT, options=["a", "b"]
            )

    def test_the_key_is_immutable_once_set(self, tenant):
        """The key is written into every stored value.

        Changing it would orphan history already captured against it, which is
        exactly the kind of silent data loss M3 exists to prevent.
        """
        field = CategoryCustomFieldFactory(key="serial_prefix")

        field.key = "prefix"
        with pytest.raises(ValidationError, match="immutable"):
            field.save()

    @pytest.mark.parametrize(
        ("field_type", "value", "valid"),
        [
            (CategoryCustomField.FieldType.NUMBER, "42", True),
            (CategoryCustomField.FieldType.NUMBER, "not a number", False),
            (CategoryCustomField.FieldType.BOOLEAN, True, True),
            (CategoryCustomField.FieldType.BOOLEAN, "yes", False),
            (CategoryCustomField.FieldType.DATE, "2026-03-01", True),
            (CategoryCustomField.FieldType.DATE, "01/03/2026", False),
            (CategoryCustomField.FieldType.TEXT, "anything", True),
        ],
    )
    def test_values_are_validated_by_type(self, tenant, field_type, value, valid):
        field = CategoryCustomFieldFactory(field_type=field_type, key="attr")

        if valid:
            field.validate_value(value)
        else:
            with pytest.raises(ValidationError):
                field.validate_value(value)

    def test_a_dropdown_value_must_be_one_of_the_options(self, tenant):
        field = CategoryCustomFieldFactory(
            field_type=CategoryCustomField.FieldType.DROPDOWN,
            key="vendor",
            options=["Huawei", "Ericsson"],
        )

        field.validate_value("Huawei")
        with pytest.raises(ValidationError, match="must be one of"):
            field.validate_value("Nokia")

    def test_a_required_field_rejects_an_empty_value(self, tenant):
        field = CategoryCustomFieldFactory(key="vendor", required_at_gate_in=True)

        with pytest.raises(ValidationError, match="required"):
            field.validate_value(None)

    def test_an_optional_field_accepts_an_empty_value(self, tenant):
        field = CategoryCustomFieldFactory(key="vendor", required_at_gate_in=False)

        field.validate_value(None)

    def test_an_item_type_sees_fields_from_its_category_and_its_parent(self, tenant):
        """Attributes set on a parent must apply to everything beneath it."""
        parent = ItemCategoryFactory(name="Active equipment")
        child = ItemCategoryFactory(name="Radios", parent=parent)
        CategoryCustomFieldFactory(category=parent, key="vendor", label="Vendor")
        CategoryCustomFieldFactory(category=child, key="band", label="Band")

        item = ItemTypeFactory(category=child)

        assert {f.key for f in item.custom_fields()} == {"vendor", "band"}

    def test_validation_reports_every_failing_field_at_once(self, tenant):
        """A storekeeper on a phone should not fix errors one at a time."""
        category = ItemCategoryFactory()
        CategoryCustomFieldFactory(
            category=category, key="vendor", label="Vendor", required_at_gate_in=True
        )
        CategoryCustomFieldFactory(
            category=category,
            key="year",
            label="Year",
            field_type=CategoryCustomField.FieldType.NUMBER,
        )
        item = ItemTypeFactory(category=category)

        with pytest.raises(ValidationError) as caught:
            item.validate_custom_field_values({"year": "not a number"})

        assert set(caught.value.message_dict) == {"vendor", "year"}

    def test_archived_fields_are_not_enforced(self, tenant):
        category = ItemCategoryFactory()
        CategoryCustomFieldFactory(
            category=category,
            key="vendor",
            required_at_gate_in=True,
            is_archived=True,
        )
        item = ItemTypeFactory(category=category)

        # Must not raise: an archived field cannot block new receipts.
        item.validate_custom_field_values({})


class TestItemType:
    """C3, D11, D12."""

    def test_tracking_mode_defaults_per_item_type(self, tenant):
        assert ItemTypeFactory().default_tracking_mode == TrackingMode.BULK

    def test_names_are_unique_within_a_tenant(self, tenant):
        ItemTypeFactory(name="RRU 2x40W")

        with pytest.raises(IntegrityError), transaction.atomic():
            ItemTypeFactory(name="RRU 2x40W")

    def test_the_same_name_may_exist_in_another_tenant(self, organization, other_organization):
        from core.tenancy import tenant_context

        with tenant_context(organization):
            ItemTypeFactory(name="RRU 2x40W")
        with tenant_context(other_organization):
            ItemTypeFactory(name="RRU 2x40W")

        with tenant_context(organization):
            assert ItemType.objects.filter(name="RRU 2x40W").count() == 1

    def test_a_returnable_item_needs_a_return_period(self, tenant):
        """I2, I3: otherwise it could never be reported overdue."""
        with pytest.raises(ValidationError, match="return period"):
            ItemTypeFactory(is_returnable=True, default_return_days=None)

    def test_a_reel_item_cannot_be_counted_in_units(self, tenant):
        """D12: a drum of cable is measured, not counted."""
        with pytest.raises(ValidationError, match="measured, not counted"):
            ItemTypeFactory(default_tracking_mode=TrackingMode.REEL, uom="ea")

    def test_a_reel_item_measured_in_metres_is_fine(self, tenant):
        item = ItemTypeFactory(default_tracking_mode=TrackingMode.REEL, uom="m")

        assert item.uom == "m"

    def test_archiving_rather_than_deleting(self, tenant):
        """C3: item types cannot be deleted once movements exist.

        The movement check lands in Phase 3 (T3.1). The archive flag it depends
        on exists now and excludes the item from pickers.
        """
        item = ItemTypeFactory(name="Obsolete RRU")
        item.is_archived = True
        item.save()

        assert ItemType.objects.filter(is_archived=False).count() == 0
        assert ItemType.objects.count() == 1


class TestStarterCatalogueSeed:
    """T2.4, C3: a new tenant starts with a usable catalogue."""

    def test_seeding_creates_the_documented_categories(self, tenant):
        seed_starter_catalogue(tenant)

        names = set(ItemCategory.objects.values_list("name", flat=True))
        assert names == {name for name, _, _ in STARTER_CATALOGUE}

    def test_the_categories_c3_names_are_all_covered(self, tenant):
        """C3 lists what must be there. This checks each one by item name."""
        seed_starter_catalogue(tenant)
        catalogue = " ".join(ItemType.objects.values_list("name", flat=True)).lower()

        for required in (
            "antenna",
            "rru",
            "bbu",
            "feeder cable",
            "jumper",
            "connector",
            "battery",
            "rectifier",
        ):
            assert required in catalogue, f"C3 requires {required} in the starter catalogue"

    def test_tools_are_seeded_as_returnable(self, tenant):
        """I1, I2: tools are issued to technicians and expected back."""
        seed_starter_catalogue(tenant)

        tools = ItemType.objects.filter(category__name="Tools")
        assert tools.exists()
        assert all(tool.is_returnable for tool in tools)
        assert all(tool.default_return_days for tool in tools)

    def test_cable_is_seeded_for_reel_tracking(self, tenant):
        """D12: reel tracking is required for cable."""
        seed_starter_catalogue(tenant)

        cable = ItemType.objects.filter(category__name="Cable and feeder")
        assert cable.exists()
        assert all(item.default_tracking_mode == TrackingMode.REEL for item in cable)
        assert all(item.uom == "m" for item in cable)

    def test_active_equipment_is_seeded_as_high_criticality(self, tenant):
        """The expensive, stealable, client-owned things need tight approval."""
        seed_starter_catalogue(tenant)

        assert (
            ItemCategory.objects.get(name="Active equipment").criticality
            == Criticality.HIGH
        )

    def test_ppe_is_not_high_criticality(self, tenant):
        """Requiring the owner to approve a pair of gloves would train staff to
        route around the approval flow entirely."""
        seed_starter_catalogue(tenant)

        assert ItemCategory.objects.get(name="PPE").criticality == Criticality.NONE

    def test_seeding_twice_adds_nothing(self, tenant):
        """A tenant provisioned before this seeder existed should be able to
        catch up without duplicating everything."""
        first = seed_starter_catalogue(tenant)
        second = seed_starter_catalogue(tenant)

        assert first["item_types"] > 0
        assert second == {"categories": 0, "item_types": 0}

    def test_the_seeded_catalogue_is_freely_editable(self, tenant):
        """D10: a tenant may rename or delete anything in it."""
        seed_starter_catalogue(tenant)

        item = ItemType.objects.get(name="Safety helmet")
        item.name = "Hard hat"
        item.save()
        item.refresh_from_db()
        assert item.name == "Hard hat"

        ItemType.objects.get(name="Work gloves").delete()
        assert not ItemType.objects.filter(name="Work gloves").exists()


class TestProvisioningSeedsTheCatalogue:
    def test_a_new_tenant_starts_with_a_catalogue(self, db):
        """T2.4 is called from T1.18, so provisioning alone must be enough."""
        from core.provisioning import provision_tenant
        from core.tenancy import tenant_context

        result = provision_tenant(
            name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
        )

        with tenant_context(result["organization"]):
            assert ItemType.objects.count() > 30
            assert ItemCategory.objects.count() == len(STARTER_CATALOGUE)
