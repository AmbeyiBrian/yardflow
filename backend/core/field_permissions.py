"""Withholding a field, rather than hiding it (§10; O14).

O14 is specific about this: a caller without the permission gets a response with
the field **absent** — not null, not zero, not blanked. Hiding a number in the
interface while the API still returns it is not a restriction; it is a
restriction-shaped thing that a browser's dev-tools tab defeats in one click.

Absent rather than null also matters for a second reason. Null reads as "there
is no figure", which is a claim about the project. Absent reads as "you were not
told", which is a claim about the reader — and only one of those is true.

Usage::

    class ProjectSerializer(PermissionGatedFieldsMixin, serializers.ModelSerializer):
        permission_gated_fields = {
            PERM.PROJECT_VIEW_COST: ("cost_to_date", "variance"),
            PERM.PROJECT_VIEW_MARGIN: ("contract_value", "margin"),
        }
"""

from __future__ import annotations

from collections.abc import Iterable


class PermissionGatedFieldsMixin:
    """Drop fields the requesting user is not permitted to see.

    Subclasses set ``permission_gated_fields`` — a mapping of permission
    codename to the field names it unlocks.

    With no request in context (a management command, a test building a
    serializer directly, an export running in Celery) **nothing is dropped**.
    Those callers have already passed whatever check applies to them, and
    silently emptying their output would be a bug that only shows up in a
    report somebody trusted.
    """

    #: {permission codename: (field names it unlocks, ...)}
    permission_gated_fields: dict[str, tuple[str, ...]] = {}

    def to_representation(self, instance):  # type: ignore[no-untyped-def]
        data = super().to_representation(instance)  # type: ignore[misc]
        for name in self._withheld_field_names():
            data.pop(name, None)
        return data

    def _withheld_field_names(self) -> Iterable[str]:
        if not self.permission_gated_fields:
            return ()

        request = self.context.get("request")  # type: ignore[attr-defined]
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return () if request is None else self._all_gated_field_names()

        from accounts.services import resolve_permissions

        permissions = resolve_permissions(user)
        withheld: list[str] = []
        for codename, field_names in self.permission_gated_fields.items():
            if not permissions.has(codename):
                withheld.extend(field_names)
        return withheld

    def _all_gated_field_names(self) -> list[str]:
        return [
            name
            for field_names in self.permission_gated_fields.values()
            for name in field_names
        ]
