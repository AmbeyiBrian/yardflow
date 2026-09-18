"""Clients, sites and projects (design §4.4; C5, C6, C7, D13, D14).

The site register is the part of this design most likely to be got wrong by
guessing. C6's rationale, quoted:

> Operator site-code formats are internal and unpublished, and towercos use
> their own. Any hardcoded format would be wrong for the second customer.

So a site carries **several labelled references** rather than one code, and the
only validation is a pattern a tenant may optionally set per client.
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel
from core.tenancy import TenantModel


class Client(TenantModel, TimeStampedModel):  # type: ignore[django-manager-missing]
    """An operator or vendor the tenant works for — Safaricom, Huawei (C5).

    D15: there is no client portal, now or planned. A client is a party material
    is attributable to, not a user of the system.
    """

    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, blank=True)

    contact_name = models.CharField(max_length=200, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)

    # C6: "an admin may optionally set a validation pattern per client, so that
    # site codes for that client are checked on entry." Optional is the
    # important word — a client with no pattern accepts anything.
    site_code_pattern = models.CharField(
        max_length=200,
        blank=True,
        help_text=(
            "Optional regular expression. When set, site references for this "
            "client are checked against it on entry."
        ),
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_client_name_per_org"
            )
        ]
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.site_code_pattern:
            # A broken pattern would reject every site reference for this client
            # with an unhelpful error, so it is validated when it is set.
            try:
                re.compile(self.site_code_pattern)
            except re.error as exc:
                raise ValidationError(
                    {"site_code_pattern": f"That is not a valid pattern: {exc}"}
                ) from exc

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization"])
        return super().save(*args, **kwargs)

    def check_site_code(self, value: str) -> None:
        """Validate a site reference against this client's pattern, if any."""
        if not self.site_code_pattern or not value:
            return
        if not re.fullmatch(self.site_code_pattern, value):
            raise ValidationError(
                f"'{value}' does not match the site code format configured for "
                f"{self.name}."
            )


class SiteType(models.TextChoices):
    GREENFIELD = "GREENFIELD", "Greenfield"
    ROOFTOP = "ROOFTOP", "Rooftop"
    INDOOR = "INDOOR", "Indoor"
    OTHER = "OTHER", "Other"


class SiteStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    # C6: decommissioned sites remain in the register permanently, because
    # recoveries originate from them (D5).
    DECOMMISSIONED = "DECOMMISSIONED", "Decommissioned"


class Site(TenantModel, TimeStampedModel):
    """A physical site where work is performed (C6)."""

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="sites")
    internal_ref = models.CharField(
        max_length=100, help_text="This company's own reference for the site."
    )
    name = models.CharField(max_length=200)

    region = models.CharField(max_length=100, blank=True)
    county = models.CharField(max_length=100, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    site_type = models.CharField(
        max_length=20, choices=SiteType.choices, default=SiteType.OTHER
    )
    status = models.CharField(
        max_length=20, choices=SiteStatus.choices, default=SiteStatus.ACTIVE, db_index=True
    )

    # C6: "optional radio identifiers may be captured as free fields, never
    # validated." Never validated is deliberate — these come off a project in
    # whatever form the operator wrote them, and rejecting one would stop work.
    cell_id = models.CharField(max_length=100, blank=True)
    enodeb_id = models.CharField(max_length=100, blank=True)

    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "internal_ref"],
                name="uniq_site_internal_ref_per_org",
            ),
        ]
        ordering = ("internal_ref",)
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "client"]),
        ]

    def __str__(self) -> str:
        return f"{self.internal_ref} — {self.name}"

    @property
    def is_decommissioned(self) -> bool:
        return self.status == SiteStatus.DECOMMISSIONED

    def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        """C6: a site is never removed from the register.

        Recoveries originate from decommissioned sites (D5), and a recovery whose
        origin had been deleted could not be shown to the operator — which is the
        one report they are most likely to ask for.
        """
        raise ValidationError(
            "Sites are never deleted. Mark the site decommissioned instead: "
            "recoveries originate from it and must stay attributable (C6, D5)."
        )


class SiteReference(TenantModel, TimeStampedModel):
    """One labelled external reference for a site (C6).

    This is what makes a single physical site matchable against an operator's
    code, a towerco's code and an internal reference at the same time — which is
    the practical problem C6 describes.
    """

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="references")
    label = models.CharField(
        max_length=100, help_text='e.g. "Safaricom site ID", "Towerco ref".'
    )
    value = models.CharField(max_length=150, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["site", "label"], name="uniq_reference_label_per_site"
            )
        ]
        ordering = ("label",)
        indexes = [
            # C6: "search across references is a single indexed lookup."
            models.Index(fields=["organization", "value"]),
        ]

    def __str__(self) -> str:
        return f"{self.label}: {self.value}"

    def clean(self) -> None:
        super().clean()
        # C6: checked against the client's pattern, when the client has one.
        if self.site_id and self.value:
            self.site.client.check_site_code(self.value)

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.full_clean(exclude=["organization"])
        return super().save(*args, **kwargs)


class ProjectStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    CLOSED = "CLOSED", "Closed"
    # O1: a PO that was awarded and then withdrawn is not the same thing as one
    # that was delivered, and its cost should not read as a delivered project's.
    CANCELLED = "CANCELLED", "Cancelled"


class Project(TenantModel, TimeStampedModel):
    """An optional grouping of work across one or more sites (C7, D14).

    D14 and C7 both stress **optional**. Material may be issued directly to a
    site, and nothing in the system may require a project to exist — a
    storekeeper under pressure will not invent one, and forcing it would produce
    fictional data.
    """

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="projects")
    reference = models.CharField(max_length=100)
    description = models.CharField(max_length=500, blank=True)
    sites = models.ManyToManyField(Site, related_name="projects", blank=True)

    # O1: the commercial layer. All of it optional, because a project without a
    # PO number is the work order this model used to be (D20) and must keep
    # working exactly as it did.
    po_number = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="The client's purchase order number. One PO is one project (D21).",
    )
    title = models.CharField(max_length=200, blank=True)

    manager = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="projects_managed",
        help_text=(
            "The project manager. Approves material leaving for this project, "
            "and is the only approver on it (O6)."
        ),
    )

    # D24: VAT-exclusive, and the help text says so because a VAT-inclusive
    # figure keyed in by mistake overstates the project by 16% and nothing
    # downstream would catch it.
    contract_value = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="What the client pays, excluding VAT.",
    )
    cost_budget = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="What the manager may spend to deliver it, excluding VAT.",
    )

    starts_on = models.DateField(null=True, blank=True)
    target_completion_on = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=20, choices=ProjectStatus.choices, default=ProjectStatus.OPEN
    )
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_with_unreconciled = models.BooleanField(
        default=False,
        help_text="Closed while material remained unreconciled, with a reason.",
    )
    close_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"], name="uniq_project_ref_per_org"
            ),
            # O1: a PO number identifies one project and one only (D21).
            # Partial, because the many projects without one are not competing
            # for the same name.
            models.UniqueConstraint(
                fields=["organization", "po_number"],
                condition=Q(po_number__gt=""),
                name="uniq_project_po_number_per_org",
            ),
            models.CheckConstraint(
                condition=Q(status=ProjectStatus.OPEN, closed_at__isnull=True)
                | Q(
                    status__in=(ProjectStatus.CLOSED, ProjectStatus.CANCELLED),
                    closed_at__isnull=False,
                ),
                name="closed_project_has_a_closing_time",
            ),
            # O1: a PO with no manager is a PO nobody can release material
            # against (O6), and one with no budget is one nobody can manage to.
            # Refused here rather than in a serializer, because a project that
            # reached the database half-specified would only be discovered at
            # the gate, by a storekeeper with a van waiting.
            models.CheckConstraint(
                condition=Q(po_number="")
                | Q(
                    manager__isnull=False,
                    contract_value__isnull=False,
                    cost_budget__isnull=False,
                ),
                name="a_po_project_is_fully_specified",
            ),
        ]
        ordering = ("-opened_at",)

    def __str__(self) -> str:
        return self.po_number or self.reference

    @property
    def is_po(self) -> bool:
        """Whether this project carries a purchase order, and so a budget (O1)."""
        return bool(self.po_number)

    def unreconciled_summary(self) -> dict:
        """What remains unaccounted for on this project (C7).

        C7 says closing "warns if material remains unreconciled". The real
        figures come from the reconciliation query in T5.7; until the ledger
        exists there is nothing to count, and reporting a confident zero would be
        worse than saying so.
        """
        try:
            from jobs.reconciliation import project_unreconciled

            return project_unreconciled(self)
        except ImportError:
            return {"available": False, "reason": "Reconciliation arrives with T5.7."}
