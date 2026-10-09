"""Asset serializers (R14, design §4.20.6, §4.20.7).

Writes go through ``assets.services``; the serializers only shape and check
the payload. The keys are the contract with ``frontend/src/features/assets``.
"""

from __future__ import annotations

from rest_framework import serializers

from accounts.permissions_registry import PERM
from assets import services
from assets.models import Asset, AssetCloseReason, AssetHandover
from core.field_permissions import PermissionGatedFieldsMixin


class AssetSerializer(PermissionGatedFieldsMixin, serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default="")
    holder_name = serializers.CharField(
        source="holder.full_name", read_only=True, default=None, allow_null=True
    )

    #: §4.20.7: what it cost is for those who keep the register or read costs.
    permission_gated_any_of = (
        (
            (PERM.ASSET_MANAGE, PERM.FINANCE_APPROVE, PERM.PROJECT_VIEW_COST),
            ("cost", "purchase_terms"),
        ),
    )

    class Meta:
        model = Asset
        fields = (
            "id",
            "type",
            "name",
            "tag",
            "purchase_date",
            "supplier",
            "supplier_name",
            "cost",
            "purchase_terms",
            "make",
            "model",
            "insurance_expires_on",
            "inspection_expires_on",
            "holder",
            "holder_name",
            "status",
            "closed_on",
            "closed_reason",
            "closed_note",
            "created_at",
        )
        read_only_fields = ("status", "closed_on", "closed_reason", "closed_note")
        # Uniqueness of the tag is the service's, so a clash answers 409 naming
        # the other asset rather than a generic 400.
        validators: list = []

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        request = self.context["request"]
        holder = validated_data.pop("holder", None)
        return services.create_asset(
            actor=request.user, holder=holder, request=request, **validated_data
        )

    def update(self, instance, validated_data):  # type: ignore[no-untyped-def]
        request = self.context["request"]
        if "holder" in validated_data and validated_data["holder"] != instance.holder:
            raise services.AssetInvalid(
                "Use Hand over to change who holds it.",
                field_errors={"holder": ["Use Hand over to change who holds it."]},
            )
        validated_data.pop("holder", None)
        return services.update_asset(
            instance, actor=request.user, request=request, **validated_data
        )


class AssetHandoverSerializer(serializers.ModelSerializer):
    from_holder_name = serializers.CharField(
        source="from_holder.full_name", read_only=True, default=None, allow_null=True
    )
    to_holder_name = serializers.CharField(
        source="to_holder.full_name", read_only=True, default=None, allow_null=True
    )
    handed_over_by_name = serializers.CharField(source="handed_over_by.full_name", read_only=True)

    class Meta:
        model = AssetHandover
        fields = (
            "id",
            "from_holder",
            "from_holder_name",
            "to_holder",
            "to_holder_name",
            "handed_over_by",
            "handed_over_by_name",
            "handed_over_on",
            "note",
        )
        read_only_fields = fields


class AssetHandOverInputSerializer(serializers.Serializer):
    """``{to_holder|null, note?, handed_over_on?}``."""

    to_holder = serializers.IntegerField(allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)
    handed_over_on = serializers.DateField(required=False)


class AssetCloseInputSerializer(serializers.Serializer):
    """``{closed_on, closed_reason, closed_note?}``."""

    closed_on = serializers.DateField()
    closed_reason = serializers.ChoiceField(choices=AssetCloseReason.choices)
    closed_note = serializers.CharField(required=False, allow_blank=True)


class AssetFuelSerializer(serializers.Serializer):
    """``GET /assets/{id}/fuel`` (§4.20.4)."""

    litres = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    spend = serializers.DecimalField(max_digits=14, decimal_places=2)
    fill_count = serializers.IntegerField()
    spend_per_litre = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    pending_spend = serializers.DecimalField(max_digits=14, decimal_places=2)


class AssetFuelSummarySerializer(AssetFuelSerializer):
    """One ranked row of ``GET /assets/fuel-summary``."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    tag = serializers.CharField()
    type = serializers.CharField()
