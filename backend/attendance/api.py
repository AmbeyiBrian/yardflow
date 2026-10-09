"""Clock-in endpoints (design §4.18.7, §4.18.8; R13).

Every write goes through ``attendance.services`` so a clock-in made here and one
replayed from a phone (R6) are judged by the same code. This module translates:
HTTP in, a service call, a read payload out. The flags on a session are derived
here, on read, from what was recorded; none is stored.

Decide, correct and the Director's add are later tasks; ``WorkDayViewSet`` keeps
the slice and visibility helpers they will use.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_field,
    inline_serializer,
)
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.api_permissions import HasPermission
from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from approvals.addressing import open_requests_addressed_to
from approvals.models import ApprovalRequest, ApprovalRequestStatus
from attendance import services
from attendance.models import (
    ClosedBy,
    CorrectionKind,
    WorkDay,
    WorkDayStatus,
    WorkSession,
    WorkSessionCorrection,
)
from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from core.audit import record
from core.models import AuditAction, Organization
from locations.models import Location
from network.models import Project, Site

#: A clock-in or clock-out that reached the server this long after it happened
#: is flagged ``SENT_LATE`` (the frontend's wording says "more than an hour").
LATE_AFTER = timedelta(hours=1)
#: A rejected slice may be corrected for this long (§4.18.6).
CORRECTION_WINDOW = timedelta(days=30)

DAY_DOCUMENT_TYPE = WorkDay._meta.label

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


# --------------------------------------------------------------------------
# Visibility (§4.18.8)
# --------------------------------------------------------------------------


def _is_director(user) -> bool:  # type: ignore[no-untyped-def]
    role_id = Organization.objects.get(pk=user.organization_id).settings.finance_director_role_id
    return role_id is not None and user.user_roles.filter(role_id=role_id).exists()


def _sees_everyone(user) -> bool:  # type: ignore[no-untyped-def]
    return resolve_permissions(user).has(PERM.ATTENDANCE_VIEW_ALL) or _is_director(user)


def _managed_days(user) -> Q:  # type: ignore[no-untyped-def]
    """Days with a session on a project ``user`` manages."""
    return Q(Exists(WorkSession.objects.filter(work_day=OuterRef("pk"), project__manager=user)))


def _awaiting_day_ids(user) -> set[int]:  # type: ignore[no-untyped-def]
    """Days with an open slice addressed to ``user`` (§4.18.7 ``awaiting_me``)."""
    requests = open_requests_addressed_to(
        user, ApprovalRequest.objects.filter(document_type=DAY_DOCUMENT_TYPE)
    )
    return {int(pk) for pk in requests.values_list("document_id", flat=True)}


def _visible_days(queryset: QuerySet, user) -> QuerySet:  # type: ignore[no-untyped-def]
    """Own days; a PM's project days; everything for view-all and the Director.

    A day with a slice waiting on the caller is visible to them whatever else
    holds (a Director-routed day need not touch a project they manage).
    """
    if _sees_everyone(user):
        return queryset
    return queryset.filter(Q(person=user) | _managed_days(user) | Q(pk__in=_awaiting_day_ids(user)))


# --------------------------------------------------------------------------
# Read shapes
# --------------------------------------------------------------------------


def _hours(start: datetime, end: datetime | None) -> Decimal | None:
    if end is None:
        return None
    seconds = Decimal(str((end - start).total_seconds()))
    return (seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _latest_edit(session: WorkSession) -> WorkSessionCorrection | None:
    edits = [c for c in session.corrections.all() if c.kind == CorrectionKind.EDIT]
    return edits[-1] if edits else None


def _counted_times(session: WorkSession) -> tuple[datetime, datetime | None]:
    """The times that count: a correction's where there is one (§4.18.6)."""
    edit = _latest_edit(session)
    if edit is None:
        return session.clock_in_at, session.clock_out_at
    return (
        edit.corrected_in_at or session.clock_in_at,
        edit.corrected_out_at or session.clock_out_at,
    )


def _place_of(session: WorkSession):  # type: ignore[no-untyped-def]
    return session.site if session.site_id is not None else session.location


def _slice_status(status: str) -> str:
    """An approval request's status as a day status."""
    if status in (ApprovalRequestStatus.APPROVED, ApprovalRequestStatus.REJECTED):
        return status
    return WorkDayStatus.PENDING


def _flags(session: WorkSession) -> list[str]:
    flags: list[str] = []
    place = _place_of(session)
    radius = float(place.radius_m) if place is not None else None
    if session.clock_out_at is not None:
        if session.out_lat is None and session.closed_by == ClosedBy.PERSON:
            flags.append("NO_POSITION_AT_CLOCK_OUT")
        elif (
            session.out_distance_m is not None
            and radius is not None
            and session.out_distance_m > radius + (session.out_accuracy_m or 0)
        ):
            flags.append("OUTSIDE_AT_CLOCK_OUT")
    if session.closed_by == ClosedBy.AUTO:
        flags.append("CLOSED_AUTOMATICALLY")
    late_in = session.clock_in_received_at - session.clock_in_at > LATE_AFTER
    late_out = (
        session.clock_out_received_at is not None
        and session.clock_out_at is not None
        and session.clock_out_received_at - session.clock_out_at > LATE_AFTER
    )
    if late_in or late_out:
        flags.append("SENT_LATE")
    if session.area_changed:
        flags.append("AREA_CHANGED")
    if session.corrections.all():
        flags.append("CORRECTED")
    if session.added_by_id is not None:
        flags.append("ADDED_BY_DIRECTOR")
    return flags


class CorrectionSerializer(serializers.ModelSerializer):
    made_by_name = serializers.SerializerMethodField()

    class Meta:
        model = WorkSessionCorrection
        fields = (
            "id",
            "kind",
            "original_in_at",
            "original_out_at",
            "corrected_in_at",
            "corrected_out_at",
            "reason",
            "made_by_name",
            "made_at",
        )
        read_only_fields = fields

    def get_made_by_name(self, correction: WorkSessionCorrection) -> str:
        return str(correction.made_by)


class WorkSessionSerializer(serializers.ModelSerializer):
    place_name = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    radius_m = serializers.SerializerMethodField()
    closed_by = serializers.SerializerMethodField()
    flags = serializers.SerializerMethodField()
    hours = serializers.SerializerMethodField()
    slice_status = serializers.SerializerMethodField()
    can_correct = serializers.SerializerMethodField()
    corrections = CorrectionSerializer(many=True, read_only=True)

    class Meta:
        model = WorkSession
        fields = (
            "id",
            "site",
            "location",
            "place_name",
            "project",
            "project_name",
            "work_day",
            "local_date",
            "clock_in_at",
            "clock_out_at",
            "in_distance_m",
            "out_distance_m",
            "radius_m",
            "closed_by",
            "in_client_uuid",
            "flags",
            "hours",
            "slice_status",
            "can_correct",
            "added_reason",
            "corrections",
        )
        read_only_fields = fields

    def get_place_name(self, session: WorkSession) -> str:
        place = _place_of(session)
        return place.name if place is not None else ""

    def get_project_name(self, session: WorkSession) -> str | None:
        project = session.project
        return project.title if project is not None else None

    def get_radius_m(self, session: WorkSession) -> int | None:
        place = _place_of(session)
        return place.radius_m if place is not None else None

    def get_closed_by(self, session: WorkSession) -> str | None:
        return session.closed_by or None

    def get_flags(self, session: WorkSession) -> list[str]:
        return _flags(session)

    def get_hours(self, session: WorkSession) -> str | None:
        hours = _hours(*_counted_times(session))
        return None if hours is None else str(hours)

    def get_slice_status(self, session: WorkSession) -> str | None:
        request = session.approval_request
        return None if request is None else _slice_status(request.status)

    def get_can_correct(self, session: WorkSession) -> bool:
        """Mine, in a rejected slice, inside the 30 days (§4.18.6)."""
        request = session.approval_request
        user = self.context["request"].user
        if request is None or request.status != ApprovalRequestStatus.REJECTED:
            return False
        if session.person_id != user.pk:
            return False
        decided = request.resolved_at or request.updated_at
        return timezone.now() - decided <= CORRECTION_WINDOW


class SliceSerializer(serializers.Serializer):
    """One approver's part of a day (§4.18.5). Empty until routing lands (T16.6)."""

    id = serializers.IntegerField()
    approver = serializers.IntegerField(allow_null=True)
    approver_name = serializers.CharField()
    project = serializers.IntegerField(allow_null=True)
    project_name = serializers.CharField(allow_null=True)
    status = serializers.CharField()
    is_mine = serializers.BooleanField()
    reason = serializers.CharField()
    decided_at = serializers.DateTimeField(allow_null=True)


def _slice_rows(days: list[WorkDay], user) -> dict[int, list[dict[str, Any]]]:  # type: ignore[no-untyped-def]
    """The slices of ``days`` in a handful of queries, keyed by day id."""
    if not days:
        return {}
    keys = {str(day.pk): day.pk for day in days}
    requests = list(
        ApprovalRequest.objects.filter(document_type=DAY_DOCUMENT_TYPE, document_id__in=list(keys))
        .select_related("required_user", "required_role")
        .prefetch_related("actions")
        .order_by("level", "id")
    )
    if not requests:
        return {}
    mine = set(
        open_requests_addressed_to(
            user, ApprovalRequest.objects.filter(pk__in=[r.pk for r in requests])
        ).values_list("pk", flat=True)
    )
    projects: dict[int, Project] = {}
    for session in WorkSession.objects.filter(
        approval_request__in=[r.pk for r in requests], project__isnull=False
    ).select_related("project"):
        projects.setdefault(session.approval_request_id, session.project)  # type: ignore[arg-type]

    out: dict[int, list[dict[str, Any]]] = {}
    for request in requests:
        project = projects.get(request.pk)
        if request.required_user_id is not None:
            approver_name = str(request.required_user)
        elif request.required_role_id is not None:
            approver_name = request.required_role.name  # type: ignore[union-attr]
        else:
            approver_name = ""
        actions = list(request.actions.all())  # newest first
        out.setdefault(keys[request.document_id], []).append(
            {
                "id": request.pk,
                "approver": request.required_user_id,
                "approver_name": approver_name,
                "project": project.pk if project is not None else None,
                "project_name": project.title if project is not None else None,
                "status": _slice_status(request.status),
                "is_mine": request.pk in mine,
                "reason": actions[0].reason if actions else "",
                "decided_at": request.resolved_at,
            }
        )
    return out


class WorkDaySerializer(serializers.ModelSerializer):
    person_name = serializers.SerializerMethodField()
    hours = serializers.SerializerMethodField()
    rejection_reason = serializers.SerializerMethodField()
    session_count = serializers.SerializerMethodField()
    sessions = WorkSessionSerializer(many=True, read_only=True)
    slices = serializers.SerializerMethodField()

    class Meta:
        model = WorkDay
        fields = (
            "id",
            "person",
            "person_name",
            "date",
            "status",
            "hours",
            "rejection_reason",
            "session_count",
            "sessions",
            "slices",
        )
        read_only_fields = fields

    def _slices_of(self, day: WorkDay) -> list[dict[str, Any]]:
        return self.context.get("slice_index", {}).get(day.pk, [])

    def get_person_name(self, day: WorkDay) -> str:
        return str(day.person)

    def get_hours(self, day: WorkDay) -> str:
        total = Decimal("0.00")
        for session in day.sessions.all():
            hours = _hours(*_counted_times(session))
            if hours is not None:
                total += hours
        return str(total)

    def get_rejection_reason(self, day: WorkDay) -> str:
        rejected = [s["reason"] for s in self._slices_of(day) if s["status"] == "REJECTED"]
        return rejected[-1] if rejected else ""

    def get_session_count(self, day: WorkDay) -> int:
        return len(day.sessions.all())

    @extend_schema_field(SliceSerializer(many=True))
    def get_slices(self, day: WorkDay) -> list[dict[str, Any]]:
        return self._slices_of(day)


class WorkDayCursorPagination(CursorPagination):
    """Newest day first (the default orders by creation, which is not the day)."""

    ordering = ("-date", "-id")
    page_size_query_param = "page_size"
    max_page_size = 200


# --------------------------------------------------------------------------
# Work days
# --------------------------------------------------------------------------

SCOPE = OpenApiParameter(
    "scope", str, enum=["mine", "team", "all"], description="Whose days. Default mine."
)
AWAITING_ME = OpenApiParameter(
    "awaiting_me", bool, description="Only days with a slice waiting on me."
)
DATE_FROM = OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD (or `from`).")
DATE_TO = OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD (or `to`).")
STATUS = OpenApiParameter("status", str, enum=[s.value for s in WorkDayStatus])
PERSON = OpenApiParameter("person", int)
PROJECT = OpenApiParameter("project", int, description="Days with a session on this project.")
SITE = OpenApiParameter("site", int, description="Days with a session at this site.")


def _int_param(params, name: str) -> int | None:  # type: ignore[no-untyped-def]
    raw = params.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        raise serializers.ValidationError({name: ["Must be a whole number."]}) from None


def _date_param(params, *names: str) -> date | None:  # type: ignore[no-untyped-def]
    for name in names:
        raw = params.get(name)
        if raw:
            parsed = parse_date(raw)
            if parsed is None:
                raise serializers.ValidationError({name: ["Use the form YYYY-MM-DD."]})
            return parsed
    return None


def _session_queryset() -> QuerySet:
    return WorkSession.objects.select_related(
        "site", "location", "project", "approval_request"
    ).prefetch_related(
        Prefetch(
            "corrections",
            queryset=WorkSessionCorrection.objects.select_related("made_by"),
        )
    )


class WorkDayViewSet(TenantScopedViewSet):
    """``/api/v1/work-days`` (R13, §4.18.7). Read only for now.

    Decide, add and the correction flow arrive with the approval task; they
    will be ``@action`` routes here, on the same visibility.
    """

    serializer_class = WorkDaySerializer
    model = WorkDay
    pagination_class = WorkDayCursorPagination
    http_method_names = ["get", "head", "options"]
    _slice_index: dict[int, list[dict[str, Any]]] | None = None

    def get_queryset(self):  # type: ignore[no-untyped-def]
        return (
            super()
            .get_queryset()
            .select_related("person")
            .prefetch_related(Prefetch("sessions", queryset=_session_queryset()))
        )

    def get_serializer_context(self) -> dict[str, Any]:
        context = super().get_serializer_context()
        context["slice_index"] = self._slice_index or {}
        return context

    def filter_queryset(self, queryset):  # type: ignore[no-untyped-def]
        queryset = super().filter_queryset(queryset)
        user = self.request.user
        params = self.request.query_params
        queryset = _visible_days(queryset, user)

        if self.action != "list":
            return queryset

        awaiting = _truthy(params.get("awaiting_me"))
        scope = params.get("scope") or (None if awaiting else "mine")
        if scope == "mine":
            queryset = queryset.filter(person=user)
        elif scope == "team":
            queryset = queryset.filter(_managed_days(user)).exclude(person=user)
        elif scope == "all":
            if not _sees_everyone(user):
                raise PermissionDenied("You cannot see everyone's days.")
        elif scope is not None:
            raise serializers.ValidationError({"scope": ["Use mine, team or all."]})

        if awaiting:
            queryset = queryset.filter(pk__in=_awaiting_day_ids(user))

        first = _date_param(params, "date_from", "from")
        last = _date_param(params, "date_to", "to")
        if first:
            queryset = queryset.filter(date__gte=first)
        if last:
            queryset = queryset.filter(date__lte=last)
        status = params.get("status")
        if status:
            if status not in WorkDayStatus.values:
                raise serializers.ValidationError({"status": ["Not a day status."]})
            queryset = queryset.filter(status=status)
        person = _int_param(params, "person")
        if person is not None:
            queryset = queryset.filter(person_id=person)
        project = _int_param(params, "project")
        if project is not None:
            queryset = queryset.filter(
                Exists(WorkSession.objects.filter(work_day=OuterRef("pk"), project_id=project))
            )
        site = _int_param(params, "site")
        if site is not None:
            queryset = queryset.filter(
                Exists(WorkSession.objects.filter(work_day=OuterRef("pk"), site_id=site))
            )
        return queryset

    def paginate_queryset(self, queryset):  # type: ignore[no-untyped-def]
        page = super().paginate_queryset(queryset)
        if page is not None:
            self._slice_index = _slice_rows(list(page), self.request.user)
        return page

    def retrieve(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        day = self.get_object()
        self._slice_index = _slice_rows([day], request.user)
        return Response(self.get_serializer(day).data)

    @extend_schema(
        parameters=[SCOPE, AWAITING_ME, DATE_FROM, DATE_TO, STATUS, PERSON, PROJECT, SITE],
        responses={200: WorkDaySerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().list(request, *args, **kwargs)


# --------------------------------------------------------------------------
# Work sessions
# --------------------------------------------------------------------------


class _AreaSerializer(serializers.Serializer):
    lat = serializers.FloatField()
    lng = serializers.FloatField()
    radius_m = serializers.FloatField()


class ClockInSerializer(serializers.Serializer):
    site = serializers.PrimaryKeyRelatedField(
        queryset=Site.objects, required=False, allow_null=True
    )
    location = serializers.PrimaryKeyRelatedField(
        queryset=Location.objects, required=False, allow_null=True
    )
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects, required=False, allow_null=True
    )
    at = serializers.DateTimeField(required=False, allow_null=True)
    # {lat, lng, accuracy_m}: judged by the service, which says why it is not
    # good enough (CLOCK_LOCATION_REQUIRED), so only its shape is checked here.
    fix = serializers.JSONField(required=False, allow_null=True)
    place_area = _AreaSerializer(required=False, allow_null=True)
    client_uuid = serializers.UUIDField(required=False, allow_null=True)


class ClockOutSerializer(serializers.Serializer):
    at = serializers.DateTimeField(required=False, allow_null=True)
    fix = serializers.JSONField(required=False, allow_null=True)
    session_client_uuid = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    client_uuid = serializers.UUIDField(required=False, allow_null=True)
    place_area = _AreaSerializer(required=False, allow_null=True)


def _fix(raw: Any) -> dict[str, Any] | None:
    return raw if isinstance(raw, dict) else None


class WorkSessionViewSet(TenantScopedViewSet):
    """``/api/v1/work-sessions`` (R13, §4.18.7).

    Reading is scoped like the days; clocking in and out is open to every
    member and acts on the caller only.
    """

    serializer_class = WorkSessionSerializer
    model = WorkSession
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        visible = _visible_days(WorkDay.objects.all(), self.request.user)
        return _session_queryset().filter(Q(person=self.request.user) | Q(work_day__in=visible))

    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise PermissionDenied("Clock in with POST /work-sessions/clock-in.")

    def _read(self, session: WorkSession) -> dict[str, Any]:
        fresh = _session_queryset().get(pk=session.pk)
        return WorkSessionSerializer(fresh, context=self.get_serializer_context()).data

    @extend_schema(
        responses={
            200: inline_serializer(
                "OpenWorkSession",
                {
                    "id": serializers.IntegerField(required=False),
                    "sessions_today": WorkSessionSerializer(many=True),
                    "accuracy_cap_m": serializers.IntegerField(),
                    "auto_close_hour": serializers.IntegerField(),
                },
            )
        }
    )
    @action(detail=False, methods=["get"], url_path="open", pagination_class=None)
    def open(self, request):  # type: ignore[no-untyped-def]
        """My open session (its fields at the top level) or none, my sessions
        today, and the limits the phone checks against (§4.18.7)."""
        settings_object = Organization.objects.get(pk=request.user.organization_id).settings
        zone = ZoneInfo(settings_object.timezone)
        today = timezone.now().astimezone(zone).date()
        mine = _session_queryset().filter(person=request.user)
        context = self.get_serializer_context()
        payload: dict[str, Any] = {}
        open_session = mine.filter(clock_out_at__isnull=True).first()
        if open_session is not None:
            payload.update(WorkSessionSerializer(open_session, context=context).data)
        payload["sessions_today"] = WorkSessionSerializer(
            mine.filter(local_date=today), many=True, context=context
        ).data
        payload["accuracy_cap_m"] = settings_object.clock_accuracy_cap_m
        payload["auto_close_hour"] = settings_object.clock_auto_close_hour
        return Response(payload)

    @extend_schema(request=ClockInSerializer, responses={201: WorkSessionSerializer})
    @action(detail=False, methods=["post"], url_path="clock-in")
    def clock_in(self, request):  # type: ignore[no-untyped-def]
        serializer = ClockInSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        area = data.get("place_area")
        session = services.clock_in(
            person=request.user,
            at=data.get("at") or timezone.now(),
            fix=_fix(data.get("fix")),
            site=data.get("site"),
            location=data.get("location"),
            project=data.get("project"),
            client_uuid=data.get("client_uuid"),
            place_area=dict(area) if area else None,
            request=request,
        )
        return Response(self._read(session), status=201)

    @extend_schema(request=ClockOutSerializer, responses={200: WorkSessionSerializer})
    @action(detail=False, methods=["post"], url_path="clock-out")
    def clock_out(self, request):  # type: ignore[no-untyped-def]
        serializer = ClockOutSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        session = services.clock_out(
            person=request.user,
            at=data.get("at") or timezone.now(),
            fix=_fix(data.get("fix")),
            client_uuid=data.get("client_uuid"),
            session_client_uuid=data.get("session_client_uuid") or None,
            request=request,
        )
        return Response(self._read(session))


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


class AttendanceSettingsSerializer(serializers.Serializer):
    clock_auto_close_hour = serializers.IntegerField(min_value=0, max_value=23)
    clock_accuracy_cap_m = serializers.IntegerField(min_value=1, max_value=100000)


def _settings_payload(settings_object) -> dict[str, int]:  # type: ignore[no-untyped-def]
    return {
        "clock_auto_close_hour": settings_object.clock_auto_close_hour,
        "clock_accuracy_cap_m": settings_object.clock_accuracy_cap_m,
    }


class AttendanceSettingsView(APIView):
    """``/api/v1/attendance/settings`` (R13, §4.18.7).

    Read by any member (the phone keeps the cap to refuse early offline);
    written with ``settings.manage``.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive, HasPermission]
    required_permissions = {"patch": PERM.SETTINGS_MANAGE}

    @staticmethod
    def _settings(request):  # type: ignore[no-untyped-def]
        return Organization.objects.get(pk=request.user.organization_id).settings

    @extend_schema(responses={200: AttendanceSettingsSerializer})
    def get(self, request):  # type: ignore[no-untyped-def]
        return Response(_settings_payload(self._settings(request)))

    @extend_schema(
        request=AttendanceSettingsSerializer(partial=True),
        responses={200: AttendanceSettingsSerializer},
    )
    def patch(self, request):  # type: ignore[no-untyped-def]
        serializer = AttendanceSettingsSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        changes = dict(serializer.validated_data)

        settings_object = self._settings(request)
        before = _settings_payload(settings_object)
        for name, value in changes.items():
            setattr(settings_object, name, value)
        settings_object.save(update_fields=list(changes) or None)

        record(
            AuditAction.SETTINGS_CHANGED,
            actor=request.user,
            organization=request.user.organization_id,
            target=settings_object,
            target_label="Attendance settings",
            request=request,
            before=before,
            after=_settings_payload(settings_object),
            note="Attendance settings changed.",
        )
        return Response(_settings_payload(settings_object))
