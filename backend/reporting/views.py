"""Reporting endpoints (design §6, §10; M1, M2).

Three endpoints for every report there will ever be:

* ``GET /reports`` — the catalogue, with each report's columns and filters, so
  T7.7's filter panels are generated rather than hand-written
* ``GET /reports/{slug}`` — the rows
* ``POST /reports/{slug}/export`` — Excel or PDF, synchronously for ordinary
  sizes and through a worker for large ones (T7.5)

None of them names a report. That is T7.1's criterion, and it is why a new report
is one class and no changes here.
"""

from __future__ import annotations

from django.http import Http404, HttpResponse
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions_registry import PERM
from accounts.services import resolve_permissions
from core.api_permissions import OrganizationIsActive
from reporting.framework import ReportError, all_reports, get_report, parse_params


class ReportCatalogueView(APIView):
    """``GET /api/v1/reports`` — every report this user may run (M1).

    Filtered by permission rather than listed in full: a report list offering
    things that 403 on the next tap teaches people to distrust the screen.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        # Named explicitly: the catalogue and one report both sit under
        # /reports, and a generated client with `reports_retrieve_2` in it is a
        # client nobody can read (N-10).
        operation_id="reports_catalogue",
        responses={
            200: inline_serializer(
                "ReportCatalogue",
                {
                    "count": serializers.IntegerField(),
                    "reports": serializers.ListField(child=serializers.DictField()),
                },
            )
        }
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        held = resolve_permissions(request.user)
        reports = [
            report.as_dict()
            for report in all_reports()
            if held.has(report.required_permission)
        ]
        return Response({"count": len(reports), "reports": reports})


class ReportView(APIView):
    """``GET /api/v1/reports/{slug}`` — the rows (M1).

    Not paginated. These are reports: an owner reading "stock on hand" wants the
    whole picture, and a page boundary in the middle of a total is worse than a
    slow request. The async export path (T7.5) is what covers the genuinely large
    ones.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        operation_id="reports_run",
        parameters=[
            OpenApiParameter(
                "any",
                description=(
                    "Each report declares its own filters; GET /reports lists them."
                ),
                required=False,
            )
        ],
        responses={
            200: inline_serializer(
                "ReportResult",
                {
                    "slug": serializers.CharField(),
                    "title": serializers.CharField(),
                    "columns": serializers.ListField(child=serializers.DictField()),
                    "rows": serializers.ListField(child=serializers.DictField()),
                    "totals": serializers.DictField(required=False),
                    "row_count": serializers.IntegerField(),
                },
            )
        },
    )
    def get(self, request, slug: str):  # type: ignore[no-untyped-def]
        report = _resolve(request, slug)
        try:
            params = parse_params(report, request.query_params)
        except ReportError as error:
            raise serializers.ValidationError({"filters": [str(error)]}) from error

        return Response(report.render(params))


class ReportExportView(APIView):
    """``POST /api/v1/reports/{slug}/export`` (M2, T7.5).

    POST rather than GET, because an export is a thing produced rather than a
    thing read — and because a queued one has a side effect (a stored file and a
    notification) that a GET should not have.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        request=inline_serializer(
            "ReportExportRequest",
            {
                "format": serializers.ChoiceField(choices=["xlsx", "pdf"]),
                "filters": serializers.DictField(required=False),
            },
        ),
        responses={
            200: OpenApiResponse(description="The file, when it was small enough."),
            202: OpenApiResponse(
                description=(
                    "Queued. The caller is notified with an expiring link when it "
                    "is ready (T7.5)."
                )
            ),
        },
    )
    def post(self, request, slug: str):  # type: ignore[no-untyped-def]
        from reporting.exports import render_export, should_run_async

        report = _resolve(request, slug)
        fmt = (request.data.get("format") or "xlsx").lower()
        if fmt not in ("xlsx", "pdf"):
            raise serializers.ValidationError(
                {"format": ["Excel (xlsx) or PDF. Those are the two M2 asks for."]}
            )

        raw_filters = request.data.get("filters") or {}
        try:
            params = parse_params(report, raw_filters)
        except ReportError as error:
            raise serializers.ValidationError({"filters": [str(error)]}) from error

        if should_run_async(report, params):
            from reporting.tasks import build_export

            build_export.delay(
                organization_id=str(request.user.organization_id),
                slug=slug,
                fmt=fmt,
                filters={key: str(value) for key, value in raw_filters.items()},
                requested_by_id=request.user.pk,
            )
            return Response(
                {
                    "queued": True,
                    "message": (
                        "This one is large, so it is being built in the "
                        "background. You will get a notification with a link "
                        "when it is ready."
                    ),
                },
                status=status.HTTP_202_ACCEPTED,
            )

        content, content_type, filename = render_export(
            slug, raw_filters, fmt=fmt, organization=request.user.organization
        )
        response = HttpResponse(content, content_type=content_type)
        # `attachment`: an export is a file somebody asked for, and a spreadsheet
        # rendered inline by the browser is not useful to anybody.
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class RetentionReviewView(APIView):
    """``GET /api/v1/retention-review`` (M5, T7.6).

    Two lists, and neither deletes anything:

    * documents past the tenant's retention period, for somebody to decide about
    * documents past it and **still open**, which is the opposite problem — those
      are not candidates for archiving, they are things nobody finished

    M5 is emphatic that "retention never silently deletes". There is deliberately
    no destructive endpoint beside this one: the review is the product.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        responses={
            200: inline_serializer(
                "RetentionReview",
                {
                    "cutoff": serializers.DateField(),
                    "retention_months": serializers.IntegerField(),
                    "candidates": serializers.ListField(child=serializers.DictField()),
                    "still_open": serializers.ListField(child=serializers.DictField()),
                },
            )
        }
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        from reporting.retention import (
            open_documents_past_retention,
            retention_cutoff,
            review_list,
        )

        held = resolve_permissions(request.user)
        if not held.has(PERM.SETTINGS_MANAGE) and not held.has(PERM.REPORT_VIEW_ALL):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                "Reviewing retention needs settings.manage or report.view_all."
            )

        organization = request.user.organization
        return Response(
            {
                "cutoff": retention_cutoff(organization),
                "retention_months": organization.settings.retention_months,
                "candidates": review_list(organization, limit=500),
                "still_open": open_documents_past_retention(organization),
                "note": (
                    "Nothing here has been changed or removed. M5: retention "
                    "never silently deletes — expiry produces this list."
                ),
            }
        )


def _resolve(request, slug: str):
    """The report, if this user may run it.

    A permission failure is a 403 while an unknown slug is a 404 — the two are
    genuinely different, and conflating them would make a typo look like a
    permissions problem.
    """
    try:
        report = get_report(slug)
    except ReportError as error:
        raise Http404(str(error)) from error

    held = resolve_permissions(request.user)
    if not held.has(report.required_permission):
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied(
            f"Running {report.title} needs the "
            f"{report.required_permission} permission."
        )
    return report


__all__ = [
    "ReportCatalogueView",
    "ReportExportView",
    "ReportView",
    "RetentionReviewView",
]
