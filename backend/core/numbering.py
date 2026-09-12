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


#: Human-facing prefixes. Changing one of these after a tenant is live would
#: make two documents look like different types, so they are fixed.
DOCUMENT_PREFIXES: dict[str, str] = {
    DocumentType.GATE_IN: "GRN",
    DocumentType.GATE_OUT: "GP",
    DocumentType.STOCK_COUNT: "SC",
    DocumentType.TRANSFER: "TR",
    DocumentType.DISPOSITION: "DP",
    DocumentType.DISPOSAL: "DS",
    DocumentType.CLIENT_RETURN: "CR",
    DocumentType.CUSTODY_TRANSFER: "CT",
}

NUMBER_WIDTH = 6


class DocumentSequence(TenantModel):
    """The next number for one document type in one organization (§4.13)."""

    document_type = models.CharField(max_length=30, choices=DocumentType.choices)
    next_number = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document_type"],
                name="uniq_sequence_per_org_and_type",
            )
        ]

    def __str__(self) -> str:
        return f"{self.document_type}: next {self.next_number}"


def format_number(document_type: str, number: int) -> str:
    """Render an allocated number, e.g. ``GP-000042``."""
    return f"{DOCUMENT_PREFIXES[document_type]}-{number:0{NUMBER_WIDTH}d}"


def allocate_number(document_type: str, *, organization_id=None) -> str:
    """Allocate the next number for ``document_type``.

    **Call this only when posting.** Allocating at draft creation would leave a
    gap for every abandoned draft (M6).

    Concurrency: the counter row is locked with ``SELECT FOR UPDATE``, so two
    simultaneous posts serialise and cannot receive the same number. Because the
    lock is held to the end of the caller's transaction, a rollback returns the
    number — which is what keeps the sequence gap-free.
    """
    if document_type not in DOCUMENT_PREFIXES:
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
    sequence.save(update_fields=["next_number"])

    return format_number(document_type, number)


def peek_next_number(document_type: str, *, organization_id=None) -> str:
    """What the next number *would* be, without consuming it.

    For showing a storekeeper what a draft will become. Never used to set a
    number — that must go through :func:`allocate_number`.
    """
    organization_id = organization_id or require_current_organization_id()
    sequence = DocumentSequence.objects.filter(
        organization_id=organization_id, document_type=document_type
    ).first()
    return format_number(document_type, sequence.next_number if sequence else 1)
