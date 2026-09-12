"""WebAuthn endpoints (design §6, §5.3; B5, F4, M3, T8.8, T8.9).

§6's table names four:

* ``POST /auth/webauthn/register/begin`` and ``/register/complete`` (T8.8)
* ``POST /auth/webauthn/assert/begin`` and ``/assert/complete`` (T8.9)

Plus the two B5 needs to avoid a lockout: listing what is enrolled, and revoking
one. Both matter — "losing a device must not lock the user out" is only true if
somebody can see which device is which and remove the right one.

The assertion challenge is raised against a **named approval request**, which is
T8.9's criterion. The endpoint therefore takes an approval id rather than working
on "the current user's next approval": a client that guessed wrong would obtain a
challenge for one document and try it on another, which is precisely the replay
being prevented.
"""

from __future__ import annotations

from django.http import Http404
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from accounts.webauthn_service import (
    begin_assertion,
    begin_registration,
    complete_registration,
    revoke_credential,
)
from core.api_permissions import OrganizationIsActive


class CredentialSerializer(serializers.Serializer):
    """What a browser hands back from ``navigator.credentials``."""

    credential = serializers.DictField()
    device_label = serializers.CharField(
        max_length=100, required=False, allow_blank=True
    )


class EnrolledCredentialSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    device_label = serializers.CharField()
    aaguid = serializers.CharField()
    is_active = serializers.BooleanField()
    last_used_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()


class RegisterBeginView(APIView):
    """``POST /api/v1/auth/webauthn/register/begin`` (B5, T8.8)."""

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        request=inline_serializer(
            "WebAuthnRegisterBegin",
            {"device_label": serializers.CharField(required=False)},
        ),
        responses={
            200: inline_serializer(
                "WebAuthnOptions",
                {"options": serializers.CharField(), "device_label": serializers.CharField()},
            )
        },
    )
    def post(self, request):  # type: ignore[no-untyped-def]
        return Response(
            begin_registration(
                request.user, device_label=request.data.get("device_label", "")
            )
        )


class RegisterCompleteView(APIView):
    """``POST /api/v1/auth/webauthn/register/complete`` (B5, T8.8)."""

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        request=CredentialSerializer, responses={201: EnrolledCredentialSerializer}
    )
    def post(self, request):  # type: ignore[no-untyped-def]
        serializer = CredentialSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        credential = complete_registration(
            request.user,
            credential=serializer.validated_data["credential"],
            device_label=serializer.validated_data.get("device_label", ""),
        )

        from core.audit import record
        from core.models import AuditAction, AuthMethod

        record(
            AuditAction.PERMISSION_CHANGED,
            actor=request.user,
            target=credential,
            target_label=credential.device_label,
            request=request,
            auth_method=AuthMethod.WEBAUTHN,
            note=(
                f"Enrolled an authenticator ({credential.device_label}) for "
                f"approval step-up (B5)."
            ),
        )
        return Response(
            _credential_payload(credential), status=status.HTTP_201_CREATED
        )


class CredentialsView(APIView):
    """``GET`` and ``DELETE`` on the caller's enrolled authenticators (B5).

    A user manages their own; an administrator with ``users.manage`` may revoke
    somebody else's, which is B5's "an admin can revoke enrolled credentials".
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(responses={200: EnrolledCredentialSerializer(many=True)})
    def get(self, request):  # type: ignore[no-untyped-def]
        from accounts.models import User, WebAuthnCredential

        user = request.user
        requested = request.query_params.get("user")
        if requested and str(requested) != str(request.user.pk):
            if not resolve_permissions(request.user).has(PERM.USERS_MANAGE):
                from rest_framework.exceptions import PermissionDenied

                raise PermissionDenied(
                    "Seeing somebody else's enrolled devices needs users.manage."
                )
            user = User.objects.filter(pk=requested).first()
            if user is None:
                # Another tenant's user is a 404, as everywhere (A3).
                raise Http404()

        credentials = WebAuthnCredential.objects.filter(user=user).order_by(
            "-created_at"
        )
        return Response(
            {"results": [_credential_payload(row) for row in credentials]}
        )

    @extend_schema(
        request=inline_serializer(
            "WebAuthnRevoke", {"credential": serializers.IntegerField()}
        ),
        responses={200: EnrolledCredentialSerializer},
    )
    def post(self, request):  # type: ignore[no-untyped-def]
        """Revoke one. POST rather than DELETE: it is not a deletion (M3).

        The credential stays, marked revoked, so an approval signed with it is
        still explicable years later.
        """
        from accounts.models import WebAuthnCredential

        credential_id = request.data.get("credential")
        credential = WebAuthnCredential.objects.filter(pk=credential_id).first()
        if credential is None:
            raise Http404()

        if credential.user_id != request.user.pk and not resolve_permissions(
            request.user
        ).has(PERM.USERS_MANAGE):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                "Revoking somebody else's authenticator needs users.manage."
            )

        revoke_credential(credential, revoked_by=request.user)

        from core.audit import record
        from core.models import AuditAction

        record(
            AuditAction.PERMISSION_CHANGED,
            actor=request.user,
            target=credential,
            target_label=credential.device_label,
            request=request,
            note=(
                f"Revoked the authenticator {credential.device_label} for "
                f"{credential.user}. They can still approve with a password (B5)."
            ),
        )
        return Response(_credential_payload(credential))


class AssertBeginView(APIView):
    """``POST /api/v1/auth/webauthn/assert/begin`` (T8.9, F4, M3).

    Takes the approval request the signature is for. The challenge is stored
    against that id and consumed on use, so an assertion cannot be replayed
    against another document — which is the criterion.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        request=inline_serializer(
            "WebAuthnAssertBegin",
            {"approval_request": serializers.IntegerField()},
        ),
        responses={
            200: inline_serializer(
                "WebAuthnAssertOptions",
                {
                    "options": serializers.CharField(),
                    "approval_request": serializers.IntegerField(),
                },
            )
        },
    )
    def post(self, request):  # type: ignore[no-untyped-def]
        from approvals.models import ApprovalRequest

        approval_id = request.data.get("approval_request")
        if not approval_id:
            raise serializers.ValidationError(
                {
                    "approval_request": [
                        "Name the approval this signature is for. A challenge "
                        "that belonged to no document could be used on any of "
                        "them (T8.9)."
                    ]
                }
            )

        approval_request = ApprovalRequest.objects.filter(pk=approval_id).first()
        if approval_request is None:
            raise Http404()

        return Response(begin_assertion(request.user, approval_request))


def _credential_payload(credential) -> dict:
    return {
        "id": credential.pk,
        "device_label": credential.device_label,
        "aaguid": credential.aaguid,
        "is_active": credential.is_active,
        "last_used_at": credential.last_used_at,
        "created_at": credential.created_at,
    }
