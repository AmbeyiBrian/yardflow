"""Document numbering (design §4.13, requirement M6).

M6: "document numbers are sequential and gap-free per tenant per document type,
so that an auditor can see nothing was removed."

Two decisions carry the requirement:

* **Numbers are allocated at posting, never at draft creation.** A storekeeper
  who starts a large delivery and abandons it must not burn a number — a gap is
  exactly what an auditor asks about, and "someone gave up halfway" is a poor
  answer.
* **A voided document keeps its number** and is marked void. Numbers are never
  reused, so the sequence stays continuous and the void is visible rather than
  hidden.

Gap-freedom rules out Postgres sequences: a sequence hands out a number outside
transaction control, so a rolled-back transaction leaves a permanent hole. A
counter row locked with ``SELECT FOR UPDATE`` is slower, and correct.
"""

from __future__ import annotations

from django.db import models, transaction

from core.tenancy import TenantModel, require_current_organization_id


class DocumentType(models.TextChoices):
    """Document types with their own independent sequence.

    The prefix is part of the type: an auditor reading GP-000042 should not have
    to look anything up to know what it is.
    """

    GATE_IN = "GATE_IN", "Goods received note"
    GATE_OUT = "GATE_OUT", "Gate pass"
    STOCK_COUNT = "STOCK_COUNT", "Stock count"
    TRANSFER = "TRANSFER", "Stock transfer"
    DISPOSITION = "DISPOSITION", "Quarantine disposition"
    DISPOSAL = "DISPOSAL", "Disposal"
    CLIENT_RETURN = "CLIENT_RETURN", "Client return waybill"
    CUSTODY_TRANSFER = "CUSTODY_TRANSFER", "Custody transfer"
    # Epic O. A project and a job are *created*, not posted — there is no draft
    # to abandon, so numbering them on creation burns nothing.
    PROJECT = "PROJECT", "Project"
    JOB = "JOB", "Job"


#: The prefix a tenant starts with. **No longer fixed:** a tenant may change it
#: in Settings, and `DocumentSequence.prefix` is then what counts.
#:
#: The original reasoning — that changing a prefix mid-life makes two documents
#: look like different types — is still true, and is now handled by telling the
#: person doing it rather than by refusing. A contractor who numbered gate
#: passes "GP" for ten years on paper has a better claim on their own scheme
#: than we do.
DEFAULT_PREFIXES: dict[str, str] = {
    DocumentType.GATE_IN: "GRN",
    DocumentType.GATE_OUT: "GP",
    DocumentType.STOCK_COUNT: "SC",
    DocumentType.TRANSFER: "TR",
    DocumentType.DISPOSITION: "DP",
    DocumentType.DISPOSAL: "DS",
    DocumentType.CLIENT_RETURN: "CR",
    DocumentType.CUSTODY_TRANSFER: "CT",
    DocumentType.PROJECT: "PRJ",
    DocumentType.JOB: "JOB",
}

#: Kept as an alias: the name is used in tests and reads better at call sites
#: that only want the starting point.
DOCUMENT_PREFIXES = DEFAULT_PREFIXES

DEFAULT_NUMBER_WIDTH = 6
NUMBER_WIDTH = DEFAULT_NUMBER_WIDTH


class DocumentSequence(TenantModel):
    """The next number for one document type in one organization (§4.13)."""

    document_type = models.CharField(max_length=30, choices=DocumentType.choices)
    next_number = models.PositiveIntegerField(default=1)

    # Configurable per tenant. Empty prefix means "use the default for this
    # type", so a row created before this existed keeps behaving as it did.
    prefix = models.CharField(
        max_length=10,
        blank=True,
        help_text="Leave empty to use the default for this document type.",
    )
    width = models.PositiveSmallIntegerField(
        default=DEFAULT_NUMBER_WIDTH,
        help_text="How many digits the number is padded to.",
    )

    #: The highest number this series has ever issued. Kept so the counter can
    #: be moved **forward** — to match a sequence a tenant already ran on paper
    #: — without being moved back onto numbers already given out. A duplicate
    #: document number is the one thing M6 cannot survive.
    highest_issued = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document_type"],
                name="uniq_sequence_per_org_and_type",
            )
        ]

    def __str__(self) -> str:
        return f"{self.document_type}: next {self.next_number}"


def format_number(
    document_type: str, number: int, *, prefix: str = "", width: int = 0
) -> str:
    """Render an allocated number, e.g. ``GP-000042``.

    ``prefix`` and ``width`` come from the tenant's series when there is one;
    without them the type's defaults apply, which is what keeps every existing
    call and every historical number reading the same.
    """
    resolved_prefix = prefix or DEFAULT_PREFIXES[document_type]
    resolved_width = width or DEFAULT_NUMBER_WIDTH
    return f"{resolved_prefix}-{number:0{resolved_width}d}"


def allocate_number(document_type: str, *, organization_id=None) -> str:
    """Allocate the next number for ``document_type``.

    **Call this only when posting.** Allocating at draft creation would leave a
    gap for every abandoned draft (M6).

    Concurrency: the counter row is locked with ``SELECT FOR UPDATE``, so two
    simultaneous posts serialise and cannot receive the same number. Because the
    lock is held to the end of the caller's transaction, a rollback returns the
    number — which is what keeps the sequence gap-free.
    """
    if document_type not in DEFAULT_PREFIXES:
        raise ValueError(f"Unknown document type '{document_type}'.")

    organization_id = organization_id or require_current_organization_id()

    # Must be inside the caller's transaction, so that allocation and the
    # document row it numbers commit or roll back together.
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "allocate_number() must be called inside a transaction, so that a "
            "failed post returns the number instead of leaving a gap (M6)."
        )

    sequence = (
        DocumentSequence.objects.select_for_update()
        .filter(organization_id=organization_id, document_type=document_type)
        .first()
    )

    if sequence is None:
        # First document of this type for this tenant. get_or_create rather than
        # create, because two concurrent first-posts would otherwise collide on
        # the unique constraint.
        sequence, _ = DocumentSequence.objects.get_or_create(
            organization_id=organization_id,
            document_type=document_type,
            defaults={"next_number": 1},
        )
        sequence = (
            DocumentSequence.objects.select_for_update()
            .filter(pk=sequence.pk)
            .get()
        )

    number = sequence.next_number
    sequence.next_number = number + 1
    sequence.highest_issued = max(sequence.highest_issued, number)
    sequence.save(update_fields=["next_number", "highest_issued"])

    return format_number(
        document_type, number, prefix=sequence.prefix, width=sequence.width
    )


def peek_next_number(document_type: str, *, organization_id=None) -> str:
    """What the next number *would* be, without consuming it.

    For showing a storekeeper what a draft will become. Never used to set a
    number — that must go through :func:`allocate_number`.
    """
    organization_id = organization_id or require_current_organization_id()
    sequence = DocumentSequence.objects.filter(
        organization_id=organization_id, document_type=document_type
    ).first()
    if sequence is None:
        return format_number(document_type, 1)
    return format_number(
        document_type,
        sequence.next_number,
        prefix=sequence.prefix,
        width=sequence.width,
    )
