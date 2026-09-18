"""Job, closeout and exception endpoints (design §6, §4.9; H1–H5, M1).

§6's convention holds here as everywhere: **state changes are POST sub-resource
actions**, never a PATCH on status. A closeout is submitted, a job is closed, a
variance is resolved — each its own action, each with its own permission.

The exceptions endpoint (T5.5) is deliberately one endpoint over several sources.
M1 asks for "an exceptions register: unresolved variances, overdue custody,
quarantined stock awaiting decision". Three separate lists would mean three
places to forget to look.
"""

from __future__ import annotations

from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from jobs.models import (
    Job,
    JobCloseout,
    JobCloseoutLine,
    JobStatus,
    Variance,
    VarianceStatus,
)
from jobs.reconciliation import reconcile_job, reconcile_project, reconcile_site
from jobs.services import close_job, resolve_variance, submit_closeout


class JobSerializer(serializers.ModelSerializer):
    site_name = serializers.CharField(source="site.name", read_only=True)
    site_ref = serializers.CharField(source="site.internal_ref", read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True)
    assignee_name = serializers.CharField(source="assignee.full_name", read_only=True)
    is_closed = serializers.BooleanField(read_only=True)
    subcontractor_name = serializers.CharField(
        source="subcontractor.name", read_only=True
    )

    class Meta:
        model = Job
        fields = (
            "id",
            "reference",
            "client",
            "client_name",
            "site",
            "site_name",
            "site_ref",
            "project",
            "delivery_mode",
            "subcontractor",
            "subcontractor_name",
            "agreed_price",
            "assignee",
            "assignee_name",
            "description",
            "status",
            "closed_at",
            "closed_by",
            "closed_with_variance",
            "close_reason",
            "is_closed",
            "created_at",
        )
        # H5: closing goes through the guard, so none of its record is writable.
        read_only_fields = (
            "status",
            "closed_at",
            "closed_by",
            "closed_with_variance",
            "close_reason",
        )

    def validate(self, attrs: dict) -> dict:
        current = {}
        if self.instance is not None:
            current = {
                "delivery_mode": self.instance.delivery_mode,
                "subcontractor": self.instance.subcontractor,
                "agreed_price": self.instance.agreed_price,
            }
        _validate_delivery({**current, **attrs})
        return attrs


class CloseJobSerializer(serializers.Serializer):
    """H5: the override needs a reason, and the reason is the point."""

    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class JobCloseoutLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item_type.name", read_only=True)
    serial_number = serializers.CharField(
        source="serial_unit.serial_number", read_only=True, default=""
    )
    drum_number = serializers.CharField(source="reel.drum_number", read_only=True, default="")

    class Meta:
        model = JobCloseoutLine
        fields = (
            "id",
            "action",
            "item_type",
            "item_name",
            "serial_unit",
            "serial_number",
            "reel",
            "drum_number",
            "quantity",
            "uom",
            "condition",
            "notes",
        )


class JobCloseoutSerializer(serializers.ModelSerializer):
    """H2: what the technician reports, in one submission.

    Lines are writable inline. A closeout arriving line by line could be
    submitted half-built from a phone on a bad connection, and half a closeout
    posts half the movements.
    """

    lines = JobCloseoutLineSerializer(many=True)
    job_reference = serializers.CharField(source="job.reference", read_only=True)
    submitted_by_name = serializers.CharField(
        source="submitted_by.full_name", read_only=True
    )

    class Meta:
        model = JobCloseout
        fields = (
            "id",
            "job",
            "job_reference",
            "submitted_by",
            "submitted_by_name",
            "on_behalf_of",
            "status",
            "submitted_at",
            "confirmed_at",
            "confirmed_by",
            "notes",
            "lines",
            "created_at",
        )
        read_only_fields = (
            "status",
            "submitted_at",
            "confirmed_at",
            "confirmed_by",
            "submitted_by",
        )

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        lines = validated_data.pop("lines")
        closeout = JobCloseout.objects.create(**validated_data)
        for line in lines:
            JobCloseoutLine.objects.create(closeout=closeout, **line)
        return closeout


class VarianceSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item_type.name", read_only=True, default="")
    difference = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)
    is_open = serializers.BooleanField(read_only=True)
    raised_by_name = serializers.CharField(
        source="raised_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = Variance
        fields = (
            "id",
            "type",
            "status",
            "job",
            "item_type",
            "item_name",
            "serial_unit",
            "expected",
            "actual",
            "difference",
            "uom",
            "reason",
            "resolution",
            "raised_by",
            "raised_by_name",
            "resolved_by",
            "resolved_at",
            "is_open",
            "created_at",
        )
        # H3: resolving is an action with a sign-off, not a field edit.
        read_only_fields = ("status", "resolution", "resolved_by", "resolved_at")


class ResolveVarianceSerializer(serializers.Serializer):
    resolution = serializers.CharField(max_length=500)
    write_off = serializers.BooleanField(default=False)


def _validate_delivery(merged: dict) -> None:
    """Say what ``delivery_mode_and_its_cost_agree`` would say (O3).

    The database refuses the pair either way. Reaching it gives an
    IntegrityError and a 500; this turns the same refusal into field errors.
    """
    from jobs.models import DeliveryMode

    subcontracted = merged.get("delivery_mode") == DeliveryMode.SUBCONTRACTED
    if subcontracted:
        missing = {
            name: "Required for a subcontracted job."
            for name in ("subcontractor", "agreed_price")
            if merged.get(name) is None
        }
        if missing:
            raise serializers.ValidationError(missing)
    else:
        extra = {
            name: "Only a subcontracted job carries this."
            for name in ("subcontractor", "agreed_price")
            if merged.get(name) is not None
        }
        if extra:
            raise serializers.ValidationError(extra)


class JobViewSet(TenantScopedViewSet):
    """``/api/v1/jobs`` (H1, H4, H5)."""

    serializer_class = JobSerializer
    model = Job
    select_related = ("site", "client", "project", "assignee", "closed_by")
    filterset_fields = ["status", "site", "client", "project"]
    # `assignee` is handled in get_queryset so it can accept "me"; leaving it in
    # filterset_fields as well would make django-filter try to cast "me" to an
    # id and 400 the request.
    search_fields = ["reference", "description", "site__name", "site__internal_ref"]
    ordering_fields = ["created_at", "reference", "status"]

    required_permissions = {
        "create": PERM.JOB_CLOSEOUT,
        "update": PERM.JOB_CLOSEOUT,
        "partial_update": PERM.JOB_CLOSEOUT,
        "close": PERM.JOB_CLOSEOUT,
    }

    # A job is closed or cancelled, never deleted (M6).
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        """``?open=true`` and ``?assignee=me`` — what the job screens ask for.

        "Still open" is three statuses, not one, and which three is a decision
        that belongs here rather than in every client: a technician's screen
        listing only ``OPEN`` would silently drop every job that had reached
        ``AWAITING_CLOSEOUT`` — which is precisely the list they came to close.
        """
        queryset = super().get_queryset()
        if self.request.query_params.get("open") == "true":
            queryset = queryset.filter(
                status__in=(
                    JobStatus.OPEN,
                    JobStatus.IN_PROGRESS,
                    JobStatus.AWAITING_CLOSEOUT,
                )
            )
        assignee = self.request.query_params.get("assignee")
        if assignee == "me":
            assignee = self.request.user.pk
        if assignee:
            queryset = queryset.filter(assignee_id=assignee)
        return queryset

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "open",
                description="true restricts to jobs still to be closed out.",
                required=False,
            ),
            OpenApiParameter(
                "assignee", description="User id, or 'me'.", required=False
            ),
        ]
    )
    def list(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().list(request, *args, **kwargs)

    @extend_schema(
        request=None,
        responses={
            200: inline_serializer(
                "JobReconciliation",
                {
                    "scope": serializers.CharField(),
                    "label": serializers.CharField(),
                    "is_reconciled": serializers.BooleanField(),
                },
            )
        },
    )
    @action(detail=True, methods=["get"])
    def reconciliation(self, request, pk=None):  # type: ignore[no-untyped-def]
        """H4: issued versus installed versus consumed versus returned."""
        return Response(reconcile_job(self.get_object()))

    @extend_schema(request=CloseJobSerializer, responses={200: JobSerializer})
    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):  # type: ignore[no-untyped-def]
        """H5: blocked while material is unaccounted for, unless overridden.

        The override is not a flag the client sets — it is a permission the user
        either holds or does not. A client that could ask for the override would
        make the control advisory.
        """
        serializer = CloseJobSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        may_override = resolve_permissions(request.user).has(
            PERM.JOB_CLOSE_WITH_VARIANCE
        )
        job = close_job(
            self.get_object(),
            closed_by=request.user,
            reason=serializer.validated_data.get("reason", ""),
            override=may_override,
            request=request,
        )
        return Response(self.get_serializer(job).data)


class JobCloseoutViewSet(TenantScopedViewSet):
    """``/api/v1/job-closeouts`` (H2, §4.9)."""

    serializer_class = JobCloseoutSerializer
    model = JobCloseout
    select_related = ("job", "job__site", "submitted_by", "on_behalf_of")
    prefetch_related = ("lines", "lines__item_type")
    filterset_fields = ["job", "status", "submitted_by"]
    ordering_fields = ["created_at"]

    required_permissions = {
        "create": PERM.JOB_CLOSEOUT,
        "update": PERM.JOB_CLOSEOUT,
        "partial_update": PERM.JOB_CLOSEOUT,
        "submit": PERM.JOB_CLOSEOUT,
    }

    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        """Q2: record who actually did it.

        A storekeeper closing out on a technician's behalf is expected, and the
        design's answer to Q2 is to make it visible in the data rather than
        assume it away — ``submitted_by`` is the real actor, ``on_behalf_of`` the
        technician.
        """
        serializer.save(created_by=self.request.user, submitted_by=self.request.user)

    @extend_schema(request=None, responses={200: JobCloseoutSerializer})
    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):  # type: ignore[no-untyped-def]
        """§4.9: installed and consumed post now; returns become expectations."""
        closeout = submit_closeout(
            self.get_object(), submitted_by=request.user, request=request
        )
        return Response(self.get_serializer(closeout).data)


class VarianceViewSet(TenantScopedViewSet):
    """``/api/v1/variances`` (H3, M1)."""

    serializer_class = VarianceSerializer
    model = Variance
    select_related = ("item_type", "job", "raised_by", "resolved_by")
    filterset_fields = ["type", "status", "job", "item_type"]
    search_fields = ["reason", "resolution"]
    ordering_fields = ["created_at", "status"]

    required_permissions = {
        "resolve": PERM.GATE_OUT_APPROVE,
    }

    # Raised by the system, resolved by a person. Never created by hand.
    http_method_names = ["get", "post", "head", "options"]

    def create(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        """A variance is a finding, not something anyone declares.

        Allowing one to be created directly would let the register be padded or
        pre-emptively resolved, which is precisely the trust it exists to carry.
        """
        return Response(
            {
                "error": {
                    "code": "METHOD_NOT_ALLOWED",
                    "message": (
                        "A variance is raised by the system when figures "
                        "disagree, and cannot be created directly (H3)."
                    ),
                }
            },
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    @extend_schema(request=ResolveVarianceSerializer, responses={200: VarianceSerializer})
    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):  # type: ignore[no-untyped-def]
        """H3: an approver signs it off, with an explanation."""
        serializer = ResolveVarianceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        variance = resolve_variance(
            self.get_object(),
            resolution=serializer.validated_data["resolution"],
            write_off=serializer.validated_data["write_off"],
            resolved_by=request.user,
            request=request,
        )
        return Response(self.get_serializer(variance).data)


class ExceptionsView(APIView):
    """``/api/v1/exceptions`` — one place to see everything unresolved (M1, T5.5).

    M1: "an exceptions register: unresolved variances, overdue custody,
    quarantined stock awaiting decision". Combining them here rather than leaving
    three lists is the point of the requirement — a register nobody can see whole
    is a register nobody works through.

    Sync exceptions join this list in T8.7, and quarantine in Phase 6; both
    appear as further entries in the same shape rather than a new endpoint.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "kind",
                description=(
                    "Restrict to one kind: variance, release_variance, custody, sync."
                ),
                required=False,
            )
        ],
        responses={
            200: inline_serializer(
                "ExceptionRegister",
                {
                    "count": serializers.IntegerField(),
                    "items": serializers.ListField(child=serializers.DictField()),
                },
            )
        },
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        kind = request.query_params.get("kind") or ""
        items: list[dict] = []

        if kind in ("", "variance"):
            items.extend(_open_variances())
        if kind in ("", "release_variance"):
            items.extend(_unacknowledged_release_variances())
        if kind in ("", "custody"):
            items.extend(_overdue_custody(request.user.organization_id))
        if kind in ("", "sync"):
            items.extend(_sync_conflicts(request.user.organization_id))

        items.sort(key=lambda entry: entry["raised_at"] or "", reverse=True)
        return Response({"count": len(items), "items": items})


def _open_variances() -> list[dict]:
    """H3: "variances appear on an exceptions report until resolved"."""
    return [
        {
            "kind": "variance",
            "id": variance.pk,
            "reference": str(variance.item_type) if variance.item_type_id else "",
            "summary": variance.reason or str(variance),
            "expected": str(variance.expected),
            "actual": str(variance.actual),
            "uom": variance.uom,
            "raised_at": variance.created_at.isoformat() if variance.created_at else None,
            "detail_url": f"/api/v1/variances/{variance.pk}",
        }
        for variance in Variance.objects.filter(
            status__in=(VarianceStatus.OPEN, VarianceStatus.INVESTIGATING)
        ).select_related("item_type")
    ]


def _unacknowledged_release_variances() -> list[dict]:
    """G1: a short load is a variance somebody has to acknowledge."""
    from dispatch.models import ReleaseVariance

    return [
        {
            "kind": "release_variance",
            "id": variance.pk,
            "reference": variance.gate_out_line.gate_out.number,
            "summary": (
                f"{variance.gate_out_line.item_type}: "
                f"{variance.approved_qty} approved, {variance.released_qty} released."
            ),
            "expected": str(variance.approved_qty),
            "actual": str(variance.released_qty),
            "uom": variance.gate_out_line.uom,
            "raised_at": variance.created_at.isoformat() if variance.created_at else None,
            "detail_url": f"/api/v1/release-variances/{variance.pk}",
        }
        for variance in ReleaseVariance.objects.filter(
            acknowledged_at__isnull=True
        ).select_related(
            "gate_out_line", "gate_out_line__gate_out", "gate_out_line__item_type"
        )
    ]


def _sync_conflicts(organization_id) -> list[dict]:
    """§8.4, T8.7: a queued document the server could not accept.

    Joined into this register rather than given a screen of its own, which is
    what T8.7 asks for and what M1 is about — "an exceptions register" is
    singular. A storekeeper working through unresolved things should not have to
    know that one of them came from a phone.
    """
    from sync.services import open_exceptions

    return [
        {
            "kind": "sync",
            "id": exception.pk,
            "reference": exception.submission.get_operation_display(),
            "summary": exception.reason,
            # A conflict is not an arithmetic disagreement, so there are no two
            # figures to compare — the payload is what makes it resolvable.
            "expected": "",
            "actual": "",
            "uom": "",
            "raised_at": (
                exception.created_at.isoformat() if exception.created_at else None
            ),
            "detail_url": f"/api/v1/sync-exceptions/{exception.pk}",
        }
        for exception in open_exceptions(organization_id)
    ]


def _overdue_custody(organization_id) -> list[dict]:
    """I3, M1: material somebody was meant to bring back and has not."""
    from custody.models import CustodyExpectation, ExpectationStatus

    return [
        {
            "kind": "custody",
            "id": expectation.pk,
            "reference": str(expectation.holder),
            "summary": (
                f"{expectation.outstanding_quantity} {expectation.item_type.uom} "
                f"{expectation.item_type} overdue from {expectation.holder} "
                f"since {expectation.expected_return_date}."
            ),
            "expected": str(expectation.quantity),
            "actual": str(expectation.returned_quantity),
            "uom": expectation.item_type.uom,
            "raised_at": (
                expectation.created_at.isoformat() if expectation.created_at else None
            ),
            "detail_url": f"/api/v1/custody-expectations/{expectation.pk}",
        }
        for expectation in CustodyExpectation.objects.filter(
            status=ExpectationStatus.OVERDUE
        ).select_related("holder", "item_type")
    ]


class ReconciliationView(APIView):
    """``/api/v1/reconciliation`` (H4, T5.7).

    Takes a site or a project and answers the operator's question. It reads
    the ledger only, so it needs no permission of its own beyond membership —
    what it can show is already limited to the tenant in context (A3).
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        parameters=[
            OpenApiParameter("site", description="Site id to reconcile.", required=False),
            OpenApiParameter(
                "project", description="Project id to reconcile.", required=False
            ),
        ],
        responses={
            200: inline_serializer(
                "Reconciliation",
                {
                    "scope": serializers.CharField(),
                    "label": serializers.CharField(),
                    "items": serializers.ListField(child=serializers.DictField()),
                    "is_reconciled": serializers.BooleanField(),
                },
            )
        },
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        from network.models import Project, Site

        site_id = request.query_params.get("site")
        project_id = request.query_params.get("project")

        if site_id:
            site = Site.objects.filter(pk=site_id).first()
            if site is None:
                raise _not_found()
            return Response(reconcile_site(site))

        if project_id:
            project = Project.objects.filter(pk=project_id).first()
            if project is None:
                raise _not_found()
            return Response(reconcile_project(project))

        raise serializers.ValidationError(
            {"site": ["Name a site or a project to reconcile (H4)."]}
        )


def _not_found():  # type: ignore[no-untyped-def]
    from django.http import Http404

    # 404 rather than 403 for another tenant's id, as everywhere else (A3).
    return Http404()


def open_jobs_for(user) -> list[Job]:
    """A technician's own list (H1, §7.4)."""
    return list(
        Job.objects.filter(
            Q(assignee=user), status__in=(JobStatus.OPEN, JobStatus.IN_PROGRESS)
        ).select_related("site", "client")
    )
