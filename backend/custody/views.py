"""Custody endpoints (design §6, §4.10; I1–I5).

§4.10: "custody balance is simply StockBalance at the holder's PERSON node — no
separate ledger." So ``/custody/holdings`` reads the ledger, not a table of its
own, and can never disagree with the stock screens.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions_registry import PERM
from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from custody.models import (
    CustodyExpectation,
    CustodyTransfer,
    CustodyTransferLine,
)
from custody.services import (
    acknowledge_transfer,
    decline_transfer,
    holdings_of,
    overdue_report,
)


class CustodyExpectationSerializer(serializers.ModelSerializer):
    holder_name = serializers.CharField(source="holder.full_name", read_only=True)
    item_name = serializers.CharField(source="item_type.name", read_only=True)
    outstanding_quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, read_only=True
    )
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = CustodyExpectation
        fields = (
            "id",
            "holder",
            "holder_name",
            "item_type",
            "item_name",
            "serial_unit",
            "reel",
            "quantity",
            "returned_quantity",
            "outstanding_quantity",
            "expected_return_date",
            "status",
            "is_open",
            "returned_at",
            "written_off_reason",
            "created_at",
        )
        # Closed by a receipt being matched, never by editing the row (H3).
        read_only_fields = fields


class CustodyTransferLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item_type.name", read_only=True)

    class Meta:
        model = CustodyTransferLine
        fields = (
            "id",
            "item_type",
            "item_name",
            "serial_unit",
            "reel",
            "quantity",
            "uom",
            "condition",
        )


class CustodyTransferSerializer(serializers.ModelSerializer):
    lines = CustodyTransferLineSerializer(many=True)
    from_holder_name = serializers.CharField(source="from_holder.full_name", read_only=True)
    to_holder_name = serializers.CharField(source="to_holder.full_name", read_only=True)

    class Meta:
        model = CustodyTransfer
        fields = (
            "id",
            "number",
            "status",
            "from_holder",
            "from_holder_name",
            "to_holder",
            "to_holder_name",
            "requested_by",
            "acknowledged_at",
            "declined_reason",
            "notes",
            "lines",
            "created_at",
        )
        read_only_fields = ("number", "status", "acknowledged_at", "declined_reason")

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        lines = validated_data.pop("lines")
        transfer = CustodyTransfer.objects.create(**validated_data)
        for line in lines:
            CustodyTransferLine.objects.create(transfer=transfer, **line)
        return transfer


class DeclineSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)


class CustodyExpectationViewSet(TenantScopedViewSet):
    """``/api/v1/custody-expectations`` (I2, I3)."""

    serializer_class = CustodyExpectationSerializer
    model = CustodyExpectation
    select_related = ("holder", "item_type", "serial_unit", "reel")
    filterset_fields = ["holder", "status", "item_type"]
    ordering_fields = ["expected_return_date", "created_at"]

    # An expectation is created by a release and closed by a receipt. Nothing
    # here writes one by hand, so the endpoint is read-only.
    http_method_names = ["get", "head", "options"]


class CustodyTransferViewSet(TenantScopedViewSet):
    """``/api/v1/custody-transfers`` (I5)."""

    serializer_class = CustodyTransferSerializer
    model = CustodyTransfer
    select_related = ("from_holder", "to_holder", "requested_by")
    prefetch_related = ("lines", "lines__item_type")
    filterset_fields = ["status", "from_holder", "to_holder"]
    ordering_fields = ["created_at"]

    required_permissions = {
        "create": PERM.CUSTODY_TRANSFER,
        "update": PERM.CUSTODY_TRANSFER,
        "partial_update": PERM.CUSTODY_TRANSFER,
        # Acknowledging and declining are deliberately *not* gated on
        # custody.transfer: the receiver has to be able to answer whether or not
        # they may raise a handover themselves (I5).
    }

    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        serializer.save(created_by=self.request.user, requested_by=self.request.user)

    @extend_schema(request=None, responses={200: CustodyTransferSerializer})
    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):  # type: ignore[no-untyped-def]
        """I5: only the receiver can accept, and the material moves then."""
        transfer = acknowledge_transfer(
            self.get_object(), actor=request.user, request=request
        )
        return Response(self.get_serializer(transfer).data)

    @extend_schema(request=DeclineSerializer, responses={200: CustodyTransferSerializer})
    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):  # type: ignore[no-untyped-def]
        """I5: refusing is a recorded answer, not silence."""
        serializer = DeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        transfer = decline_transfer(
            self.get_object(),
            actor=request.user,
            reason=serializer.validated_data["reason"],
            request=request,
        )
        return Response(self.get_serializer(transfer).data)


class CustodyHoldingsView(APIView):
    """``/api/v1/custody/holdings`` — what one person holds (I1).

    Defaults to the caller, so a technician's "my custody" screen needs no
    permission of its own. Naming another holder answers I4's "who is holding
    what", which is the same question an owner asks.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "holder", description="User id; defaults to the caller.", required=False
            )
        ],
        responses={
            200: inline_serializer(
                "CustodyHoldings",
                {
                    "holder": serializers.CharField(),
                    "items": serializers.ListField(child=serializers.DictField()),
                },
            )
        },
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        from accounts.models import User

        holder_id = request.query_params.get("holder")
        if holder_id and str(holder_id) != str(request.user.pk):
            holder = User.objects.filter(pk=holder_id).first()
            if holder is None:
                from django.http import Http404

                # Another tenant's user is a 404, as everywhere (A3).
                raise Http404()
        else:
            holder = request.user

        items = holdings_of(holder)
        return Response(
            {
                "holder": str(holder),
                "holder_id": holder.pk,
                "items": [
                    {**item, "quantity": str(item["quantity"])} for item in items
                ],
            }
        )


class OverdueCustodyView(APIView):
    """``/api/v1/custody/overdue`` (I4).

    Both groupings, because they answer different questions: "who keeps doing
    this?" and "what do we keep losing?".
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "search",
                description="Match a person or an item across both groupings.",
                required=False,
            )
        ],
        responses={
            200: inline_serializer(
                "OverdueCustody",
                {
                    "by_person": serializers.ListField(child=serializers.DictField()),
                    "by_item": serializers.ListField(child=serializers.DictField()),
                    "total": serializers.IntegerField(),
                },
            )
        },
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        report = overdue_report(request.user.organization_id)

        # I3: the question this answers is "who has my kit". Searched over the
        # grouped rows, because that is what the report *is* — grouping first
        # and filtering after would leave totals that do not add up to the rows
        # underneath them.
        search = (request.query_params.get("search") or "").strip().lower()

        def matches(row: dict) -> bool:
            if not search:
                return True
            # Only the text on the row. Comparing against the counts would let
            # a search for "3" match a person holding three items, which is not
            # what anybody typing into a search box means.
            return any(
                search in value.lower()
                for value in row.values()
                if isinstance(value, str)
            )

        by_person = [
            {**row, "quantity": str(row["quantity"])}
            for row in report["by_person"]
            if matches(row)
        ]
        by_item = [
            {**row, "quantity": str(row["quantity"])}
            for row in report["by_item"]
            if matches(row)
        ]

        return Response(
            {
                "as_at": report["as_at"],
                "total": report["total"],
                "by_person": by_person,
                "by_item": by_item,
            }
        )
