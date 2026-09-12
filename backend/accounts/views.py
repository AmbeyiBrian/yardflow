"""Identity endpoints (design §6).

Requirements B1 (login by email or phone, revocable refresh), B4 (resolved
permissions drive the client).
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenRefreshView

from accounts.serializers import (
    LoginSerializer,
    LogoutSerializer,
    MeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PermissionCatalogueSerializer,
)


class LoginView(APIView):
    """``POST /api/v1/auth/login`` — email or phone plus password (B1)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = LoginSerializer

    @extend_schema(request=LoginSerializer, responses={200: MeSerializer})
    def post(self, request):  # type: ignore[no-untyped-def]
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save(), status=status.HTTP_200_OK)


class RefreshView(TokenRefreshView):
    """``POST /api/v1/auth/refresh``.

    Rotation and blacklist-after-rotation are on (see SIMPLE_JWT), so a stolen
    refresh token stops working as soon as the legitimate holder uses theirs.
    """

    # simplejwt's stubs declare these as empty tuples, which no real override
    # can satisfy; the values themselves are correct.
    permission_classes = (AllowAny,)  # type: ignore[assignment]
    authentication_classes = ()


class LogoutView(APIView):
    """``POST /api/v1/auth/logout`` — revoke the refresh token (B1)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=LogoutSerializer, responses={204: None})
    def post(self, request):  # type: ignore[no-untyped-def]
        serializer = LogoutSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()

        from django.contrib.auth.signals import user_logged_out

        user_logged_out.send(sender=request.user.__class__, request=request, user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """``GET /api/v1/me`` — user, roles, resolved permissions, org settings (§7.2)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: MeSerializer})
    def get(self, request):  # type: ignore[no-untyped-def]
        return Response(MeSerializer(request.user, context={"request": request}).data)


class PermissionCatalogueView(APIView):
    """``GET /api/v1/permissions`` — every permission a role may hold (B4).

    Served from the code registry, so the role editor can never offer a
    permission nothing checks.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: PermissionCatalogueSerializer(many=True)})
    def get(self, request):  # type: ignore[no-untyped-def]
        return Response(PermissionCatalogueSerializer.catalogue())


class PasswordResetRequestView(APIView):
    """``POST /api/v1/auth/password-reset`` (B2)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(request=PasswordResetRequestSerializer, responses={200: None})
    def post(self, request):  # type: ignore[no-untyped-def]
        serializer = PasswordResetRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response(serializer.save(), status=status.HTTP_200_OK)


class PasswordResetConfirmView(APIView):
    """``POST /api/v1/auth/password-reset/confirm`` (B2, and A1's invitation)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(request=PasswordResetConfirmSerializer, responses={204: None})
    def post(self, request):  # type: ignore[no-untyped-def]
        serializer = PasswordResetConfirmSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_204_NO_CONTENT)
