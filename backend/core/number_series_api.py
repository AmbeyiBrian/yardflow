"""Number series, as a tenant can set them (§4.13, §6; M6).

One row per document type, created on demand by the first allocation. A type a
tenant has never used has no row yet, so the list fills in the defaults rather
than hiding the type — a settings screen that only shows what you have already
done is no use for deciding what the next one should look like.

**The counter only moves forward.** A tenant may set it ahead, which is the real
case: a contractor who numbered gate passes to 4,312 on paper wants to carry on
from there. Moving it back would hand out a number already issued, and a
duplicate document number is the one thing M6's guarantee cannot survive — so
that is refused, with the reason.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.api_permissions import HasPermission
from accounts.permissions_registry import PERM
from core.api_permissions import OrganizationIsActive
from core.numbering import (
    DEFAULT_NUMBER_WIDTH,
    DEFAULT_PREFIXES,
    DocumentSequence,
    DocumentType,
    format_number,
)
from core.tenancy import require_current_organization_id


class NumberSeriesSerializer(serializers.Serializer):
    document_type = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    prefix = serializers.CharField(max_length=10, allow_blank=True)
    width = serializers.IntegerField(min_value=1, max_value=12)
    next_number = serializers.IntegerField(min_value=1)
    highest_issued = serializers.IntegerField(read_only=True)
    example = serializers.CharField(read_only=True)


def _as_dict(document_type: str, sequence: DocumentSequence | None) -> dict:
    prefix = sequence.prefix if sequence and sequence.prefix else DEFAULT_PREFIXES[document_type]
    width = sequence.width if sequence else DEFAULT_NUMBER_WIDTH
    next_number = sequence.next_number if sequence else 1
    return {
        "document_type": document_type,
        "label": DocumentType(document_type).label,
        "prefix": prefix,
        "width": width,
        "next_number": next_number,
        "highest_issued": sequence.highest_issued if sequence else 0,
        "example": format_number(document_type, next_number, prefix=prefix, width=width),
    }


class NumberSeriesView(APIView):
    """``/api/v1/number-series`` — every document type and how it is numbered."""

    permission_classes = [IsAuthenticated, OrganizationIsActive, HasPermission]
    required_permissions = {"get": PERM.SETTINGS_MANAGE, "patch": PERM.SETTINGS_MANAGE}

    @extend_schema(responses={200: NumberSeriesSerializer(many=True)})
    def get(self, request):  # type: ignore[no-untyped-def]
        organization_id = require_current_organization_id()
        rows = {
            sequence.document_type: sequence
            for sequence in DocumentSequence.objects.filter(
                organization_id=organization_id
            )
        }
        return Response(
            [_as_dict(value, rows.get(value)) for value, _ in DocumentType.choices]
        )

    @extend_schema(
        request=NumberSeriesSerializer, responses={200: NumberSeriesSerializer}
    )
    def patch(self, request):  # type: ignore[no-untyped-def]
        organization_id = require_current_organization_id()

        document_type = request.data.get("document_type")
        if document_type not in DEFAULT_PREFIXES:
            return Response(
                {"document_type": "Unknown document type."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = NumberSeriesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        sequence, _ = DocumentSequence.objects.get_or_create(
            organization_id=organization_id,
            document_type=document_type,
            defaults={"next_number": 1},
        )

        # Forward only. Setting it back would re-issue numbers already on
        # documents somebody has filed (M6).
        floor = max(sequence.highest_issued + 1, 1)
        if data["next_number"] < floor:
            return Response(
                {
                    "next_number": (
                        f"This series has already issued up to "
                        f"{sequence.highest_issued}. The next number can move "
                        f"forward but not back — going back would give two "
                        f"documents the same number."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        sequence.prefix = data["prefix"].strip()
        sequence.width = data["width"]
        sequence.next_number = data["next_number"]
        sequence.save(update_fields=["prefix", "width", "next_number"])

        return Response(_as_dict(document_type, sequence))
