"""The tenant's own company profile and logo (A4).

The logo field and the document templates that render it both existed from the
start; nothing let a customer upload one. Their branding therefore depended on
the platform owner opening the Django admin on their behalf, which is not
administration of your own company — it is a support ticket for a picture.

Two boundaries worth stating, because they are easy to blur:

**Their documents carry their mark.** A gate pass is the customer's document —
their client reads it, their driver carries it — so the header is theirs. It is
capped at 18 mm by 45 mm, which is what the print template allows.

**The product keeps its own.** YardFlow's wordmark stays in the app shell and in
the "Produced by" line at the foot of every document. A tenant brands what is
theirs, not the tool they are using.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.api_permissions import HasPermission
from accounts.permissions_registry import PERM
from core.api_permissions import OrganizationIsActive
from core.models import AuditAction, Organization

#: What the print templates allow, in millimetres. Stated here so the interface
#: and the document cannot drift apart.
PRINT_HEIGHT_MM = 18
PRINT_WIDTH_MM = 45

#: A logo has to survive a mono laser printer at 18 mm. Below this it prints as
#: a smudge, and the person who uploaded it will never know unless told now.
MIN_LONG_EDGE_PX = 300
MAX_LONG_EDGE_PX = 4000
MAX_BYTES = 3 * 1024 * 1024

ACCEPTED = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/svg+xml": "SVG",
}


class OrganizationProfileSerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = (
            "name",
            "legal_name",
            "email",
            "phone",
            "address",
            "tax_pin",
            "logo",
            "logo_url",
        )
        extra_kwargs = {"logo": {"write_only": True, "required": False}}

    def get_logo_url(self, organization) -> str:
        """An address the browser can actually fetch.

        Storage answers differently in each environment: in production it is a
        pre-signed S3 link, already absolute and good for a few minutes; in
        development it is a bare ``/media/...`` path served by Django on port
        8000, which the app on 5173 cannot reach. Resolving it against the
        request makes both cases the same thing — a URL — and leaves an
        already-absolute one untouched.
        """
        if not organization.logo:
            return ""
        url = organization.logo.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url

    def validate_logo(self, uploaded):  # type: ignore[no-untyped-def]
        """Refuse what will not print, and say why before it is saved."""
        if uploaded in (None, ""):
            return uploaded

        content_type = getattr(uploaded, "content_type", "") or ""
        if content_type not in ACCEPTED:
            raise serializers.ValidationError(
                f"That is a {content_type or 'file of unknown type'}. Use a PNG, a JPEG or an SVG."
            )

        if uploaded.size > MAX_BYTES:
            raise serializers.ValidationError(
                f"That file is {uploaded.size / 1024 / 1024:.1f} MB. Keep it under "
                "3 MB — it only ever prints at 18 mm tall."
            )

        if content_type == "image/svg+xml":
            # Vector: it prints perfectly at any size, so there is nothing to
            # measure. The best thing somebody can upload here.
            return uploaded

        try:
            from PIL import Image

            image = Image.open(uploaded)
            image.verify()
            width, height = image.size
        except Exception:
            raise serializers.ValidationError("That file could not be read as an image.") from None
        finally:
            uploaded.seek(0)

        long_edge = max(width, height)
        if long_edge < MIN_LONG_EDGE_PX:
            raise serializers.ValidationError(
                f"That image is {width}×{height}. It needs at least "
                f"{MIN_LONG_EDGE_PX} px on its longest side, or it prints blurred "
                "on a gate pass."
            )
        if long_edge > MAX_LONG_EDGE_PX:
            raise serializers.ValidationError(
                f"That image is {width}×{height}, which is far larger than the "
                f"{PRINT_WIDTH_MM} mm it prints at. Something around 900×360 is ideal."
            )
        return uploaded


class OrganizationProfileView(APIView):
    """``/api/v1/organization`` — the company's own details and logo (A4).

    Readable by any member: the name and logo appear on documents they generate,
    so a screen showing a preview must be able to read them. Writing is behind
    ``settings.manage``, the same gate as everything else that changes what the
    company looks like from outside.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive, HasPermission]
    required_permissions = {"patch": PERM.SETTINGS_MANAGE, "delete": PERM.SETTINGS_MANAGE}
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_organization(self, request):  # type: ignore[no-untyped-def]
        return Organization.objects.get(pk=request.user.organization_id)

    @extend_schema(responses={200: OrganizationProfileSerializer})
    def get(self, request):  # type: ignore[no-untyped-def]
        organization = self.get_organization(request)
        data = OrganizationProfileSerializer(organization, context={"request": request}).data
        # The interface should not have to hard-code what the print template
        # allows; drift between the two is how a logo ends up cropped.
        data["logo_guidance"] = {
            "print_height_mm": PRINT_HEIGHT_MM,
            "print_width_mm": PRINT_WIDTH_MM,
            "accepted": sorted(ACCEPTED.values()),
            "max_bytes": MAX_BYTES,
            "min_long_edge_px": MIN_LONG_EDGE_PX,
            "recommended": "About 900 × 360 px, PNG with a transparent background.",
        }
        return Response(data)

    @extend_schema(
        request=OrganizationProfileSerializer,
        responses={200: OrganizationProfileSerializer},
    )
    def patch(self, request):  # type: ignore[no-untyped-def]
        organization = self.get_organization(request)
        serializer = OrganizationProfileSerializer(organization, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        from core.audit import record

        record(
            AuditAction.SETTINGS_CHANGED,
            actor=request.user,
            organization=request.user.organization_id,
            target=organization,
            target_label="Company profile",
            request=request,
            note="Company details or logo changed.",
        )
        return Response(
            OrganizationProfileSerializer(organization, context={"request": request}).data
        )

    @extend_schema(
        request=None,
        responses={204: OpenApiResponse(description="The logo was removed.")},
    )
    def delete(self, request):  # type: ignore[no-untyped-def]
        """Remove the logo. The documents fall back to the company name."""
        organization = self.get_organization(request)
        if organization.logo:
            organization.logo.delete(save=True)
        return Response(status=status.HTTP_204_NO_CONTENT)
