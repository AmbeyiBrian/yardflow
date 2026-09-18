"""Project performance, as seen by whoever is asking (§10; O12, O14).

Its own serializer rather than fields on ``ProjectSerializer``, because every
figure here is a query. Computing them per row of a list would run one set of
aggregates per project on a screen that only wanted names.
"""

from __future__ import annotations

from rest_framework import serializers

from commercials.visibility import may_see_project_cost, may_see_project_margin


class ProjectPerformanceSerializer(serializers.Serializer):
    """One project's cost against its value (O12).

    Fields are **removed** for a viewer who may not see them, never blanked.
    A null margin is a claim about the project; an absent one is a claim about
    the reader, and only the second is true (§10).
    """

    #: O14: the owner's half.
    MARGIN_FIELDS = ("contract_value", "margin", "margin_percent")
    #: O14: the manager's half, scoped to projects they manage.
    COST_FIELDS = (
        "material",
        "material_loss",
        "subcontractor",
        "labour",
        "expenses",
        "cost_to_date",
        "exposure",
        "cost_budget",
        "budget_variance",
        "is_over_budget",
        "is_fully_valued",
        "unvalued_movements",
        "uncosted_labour_entries",
        "jobs_closed_without_labour",
    )

    def to_representation(self, performance):  # type: ignore[no-untyped-def]
        cost = performance.cost
        data = {
            "project": performance.project.pk,
            "reference": str(performance.project),
            "status": performance.project.status,
            "manager": performance.project.manager_id,
            # Progress is quantities, not money, so everybody who can see the
            # project sees it.
            "jobs_total": cost.jobs_total,
            "jobs_closed": cost.jobs_closed,
            "progress_percent": _text(performance.progress_percent),
            "material": _text(cost.material),
            "material_loss": _text(cost.material_loss),
            "subcontractor": _text(cost.subcontractor),
            "labour": _text(cost.labour),
            "expenses": _text(cost.expenses),
            "cost_to_date": _text(cost.total),
            "exposure": _text(cost.exposure),
            "is_fully_valued": cost.is_fully_valued,
            "unvalued_movements": cost.unvalued_movements,
            "uncosted_labour_entries": cost.uncosted_labour_entries,
            "jobs_closed_without_labour": cost.jobs_closed_without_labour,
            "cost_budget": _text(performance.cost_budget),
            "budget_variance": _text(performance.budget_variance),
            "is_over_budget": performance.is_over_budget,
            "contract_value": _text(performance.contract_value),
            "margin": _text(performance.margin),
            "margin_percent": _text(performance.margin_percent),
        }

        request = self.context.get("request")
        if not may_see_project_margin(request):
            for name in self.MARGIN_FIELDS:
                data.pop(name, None)
        if not may_see_project_cost(request, performance.project):
            for name in self.COST_FIELDS:
                data.pop(name, None)
        return data


def _text(value):  # type: ignore[no-untyped-def]
    """Decimals as strings, so no client rounds money through a float."""
    return None if value is None else str(value)
