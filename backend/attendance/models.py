"""Clock-in records (design §4.18.2, R13).

Tables only: the services that clock in, route and correct arrive in later
tasks. What lives here is what the *tables* refuse, so a later bug cannot
quietly rewrite a day somebody has already decided.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from core.immutability import AppendOnlyModel
from core.models import TimeStampedModel
from core.tenancy import TenantModel


class ClosedBy(models.TextChoices):
    PERSON = "PERSON", "The person"
    NEXT_CLOCK_IN = "NEXT_CLOCK_IN", "Their next clock-in"
    AUTO = "AUTO", "Closed automatically"


class WorkDayStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    PENDING = "PENDING", "Waiting on approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class CorrectionKind(models.TextChoices):
    EDIT = "EDIT", "Edit an existing session"
    ADD = "ADD", "Add a missing session"


class WorkDay(TenantModel, TimeStampedModel):
    """One person's day (§4.18.2): a projection of its slices' requests."""

    person = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="work_days"
    )
    date = models.DateField()
    status: models.CharField = models.CharField(
        max_length=20,
        choices=WorkDayStatus.choices,
        default=WorkDayStatus.OPEN,
        db_index=True,
    )
    formed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "person", "date"],
                name="uniq_work_day_per_person_date",
            )
        ]
        ordering = ("-date",)

    def __str__(self) -> str:
        return f"{self.person_id} {self.date}"

    # The approval engine reads these two names off any document (§4.18.2).
    @property
    def requested_by_id(self) -> int:
        return self.person_id

    @property
    def recorded_by_id(self) -> int:
        return self.person_id


class WorkSession(TenantModel, TimeStampedModel):
    """One stretch at one place (§4.18.2).

    Immutable once its request is decided. A ``REJECTED`` request still lets
    the session be repointed at the request a correction reopens (§4.18.6);
    recorded times are never overwritten, the correction row holds the new ones.
    """

    #: What may still change on a session whose request was rejected: it is
    #: repointed at the reopened request (§4.18.6).
    REJECTED_FIELDS: frozenset[str] = frozenset({"approval_request_id", "updated_at"})

    person = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="work_sessions"
    )
    site = models.ForeignKey(
        "network.Site",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_sessions",
    )
    location = models.ForeignKey(
        "locations.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_sessions",
    )
    project = models.ForeignKey(
        "network.Project",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_sessions",
    )
    work_day = models.ForeignKey(
        WorkDay, on_delete=models.PROTECT, related_name="sessions"
    )
    #: In the organization's timezone (§4.18.2).
    local_date = models.DateField(db_index=True)

    clock_in_at = models.DateTimeField()
    clock_in_received_at = models.DateTimeField()
    in_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    in_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    in_accuracy_m = models.FloatField(null=True, blank=True)
    in_distance_m = models.FloatField(null=True, blank=True)

    clock_out_at = models.DateTimeField(null=True, blank=True)
    clock_out_received_at = models.DateTimeField(null=True, blank=True)
    out_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    out_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    out_accuracy_m = models.FloatField(null=True, blank=True)
    out_distance_m = models.FloatField(null=True, blank=True)

    closed_by = models.CharField(
        max_length=20, choices=ClosedBy.choices, blank=True, default=""
    )
    in_client_uuid = models.UUIDField(null=True, blank=True)
    out_client_uuid = models.UUIDField(null=True, blank=True)

    #: What the phone checked against; set only on offline replay (§4.18.4).
    in_checked_area = models.JSONField(null=True, blank=True)
    area_changed = models.BooleanField(default=False)

    approval_request = models.ForeignKey(
        "approvals.ApprovalRequest",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_sessions",
    )

    # 4.18.6a: a day the Director added for someone.
    added_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    added_reason = models.CharField(max_length=500, blank=True)

    _loaded_state: dict[str, Any] | None = None

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(site__isnull=False) & Q(location__isnull=True))
                | (Q(site__isnull=True) & Q(location__isnull=False)),
                name="work_session_exactly_one_place",
            ),
            models.CheckConstraint(
                condition=Q(clock_out_at__isnull=True)
                | Q(clock_out_at__gte=F("clock_in_at")),
                name="work_session_out_not_before_in",
            ),
            models.UniqueConstraint(
                fields=["organization", "person"],
                condition=Q(clock_out_at__isnull=True),
                name="uniq_open_work_session_per_person",
            ),
            models.UniqueConstraint(
                fields=["organization", "in_client_uuid"],
                condition=Q(in_client_uuid__isnull=False),
                name="uniq_work_session_in_uuid_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "out_client_uuid"],
                condition=Q(out_client_uuid__isnull=False),
                name="uniq_work_session_out_uuid_per_org",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "person", "local_date"]),
        ]
        ordering = ("clock_in_at",)

    def __str__(self) -> str:
        return f"{self.person_id} {self.clock_in_at:%Y-%m-%d %H:%M}"

    @classmethod
    def from_db(cls, db, field_names, values):  # type: ignore[no-untyped-def]
        instance = super().from_db(db, field_names, values)
        instance._loaded_state = {
            field.attname: value
            for field, value in zip(cls._meta.concrete_fields, values, strict=False)
            if field.attname in field_names
        }
        return instance

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._guard_changes()
        result = super().save(*args, **kwargs)
        self._loaded_state = {
            field.attname: getattr(self, field.attname)
            for field in self._meta.concrete_fields
        }
        return result

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self._stored_request_status() in ("APPROVED", "REJECTED"):
            raise ValidationError(
                "A work session whose request is decided cannot be deleted (§4.18.2)."
            )
        return super().delete(*args, **kwargs)

    def _stored_request_status(self) -> str | None:
        """The status of the request the row was loaded pointing at."""
        state = self._loaded_state
        if state is None or state.get("approval_request_id") is None:
            return None
        from approvals.models import ApprovalRequest

        return (
            ApprovalRequest.objects.filter(pk=state["approval_request_id"])
            .values_list("status", flat=True)
            .first()
        )

    def _guard_changes(self) -> None:
        state = self._loaded_state
        if state is None or self._state.adding:
            return
        status = self._stored_request_status()
        if status not in ("APPROVED", "REJECTED"):
            return
        allowed = self.REJECTED_FIELDS if status == "REJECTED" else {"updated_at"}
        changed = [
            field.attname
            for field in self._meta.concrete_fields
            if field.attname in state
            and field.attname not in allowed
            and getattr(self, field.attname) != state[field.attname]
        ]
        if changed:
            raise ValidationError(
                f"A work session whose request is {status.lower()} cannot be "
                f"changed ({', '.join(changed)}) — record a correction instead "
                "(§4.18.6)."
            )


class WorkSessionCorrection(AppendOnlyModel, TenantModel, TimeStampedModel):
    """A person's statement correcting a rejected session (§4.18.6).

    Append-only, in the database as well (trigger). The session keeps its
    original times; the latest correction's apply.
    """

    append_only_reason = "correct a session by adding another correction row"

    session = models.ForeignKey(
        WorkSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="corrections",
    )
    work_day = models.ForeignKey(
        WorkDay, on_delete=models.PROTECT, related_name="corrections"
    )
    kind = models.CharField(max_length=10, choices=CorrectionKind.choices)
    site = models.ForeignKey(
        "network.Site", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    location = models.ForeignKey(
        "locations.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    original_in_at = models.DateTimeField(null=True, blank=True)
    original_out_at = models.DateTimeField(null=True, blank=True)
    corrected_in_at = models.DateTimeField(null=True, blank=True)
    corrected_out_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=500)
    made_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    made_at = models.DateTimeField()
    rejected_request = models.ForeignKey(
        "approvals.ApprovalRequest",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    reopened_request = models.ForeignKey(
        "approvals.ApprovalRequest",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        constraints = [
            # EDIT names a session and no place; ADD names a place and no session.
            models.CheckConstraint(
                condition=(
                    Q(kind="EDIT")
                    & Q(session__isnull=False)
                    & Q(site__isnull=True)
                    & Q(location__isnull=True)
                )
                | (
                    Q(kind="ADD")
                    & Q(session__isnull=True)
                    & (
                        (Q(site__isnull=False) & Q(location__isnull=True))
                        | (Q(site__isnull=True) & Q(location__isnull=False))
                    )
                ),
                name="work_correction_kind_matches_target",
            )
        ]
        ordering = ("made_at", "pk")
