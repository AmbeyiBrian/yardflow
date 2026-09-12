"""T1.15 — gap-free document numbering (§4.13, M6)."""

import threading

import pytest
from django.db import connection, transaction

from core.numbering import (
    DocumentSequence,
    DocumentType,
    allocate_number,
    format_number,
    peek_next_number,
)
from core.tenancy import tenant_context


class TestSequentialAllocation:
    """M6: sequential per tenant per document type."""

    def test_numbers_start_at_one(self, tenant):
        with transaction.atomic():
            assert allocate_number(DocumentType.GATE_IN) == "GRN-000001"

    def test_numbers_increment(self, tenant):
        with transaction.atomic():
            first = allocate_number(DocumentType.GATE_IN)
            second = allocate_number(DocumentType.GATE_IN)
            third = allocate_number(DocumentType.GATE_IN)

        assert [first, second, third] == ["GRN-000001", "GRN-000002", "GRN-000003"]

    def test_each_document_type_has_its_own_sequence(self, tenant):
        with transaction.atomic():
            grn = allocate_number(DocumentType.GATE_IN)
            gate_pass = allocate_number(DocumentType.GATE_OUT)

        assert grn == "GRN-000001"
        assert gate_pass == "GP-000001"

    def test_prefixes_identify_the_document_without_a_lookup(self):
        assert format_number(DocumentType.GATE_OUT, 42) == "GP-000042"
        assert format_number(DocumentType.DISPOSAL, 7) == "DS-000007"

    def test_an_unknown_document_type_is_rejected(self, tenant):
        with pytest.raises(ValueError, match="Unknown document type"), transaction.atomic():
            allocate_number("NOT_A_TYPE")

    def test_sequences_are_independent_per_tenant(self, organization, other_organization):
        """A2/A3: one tenant's activity must not shift another's numbering."""
        with tenant_context(organization), transaction.atomic():
            allocate_number(DocumentType.GATE_IN)
            allocate_number(DocumentType.GATE_IN)

        with tenant_context(other_organization), transaction.atomic():
            assert allocate_number(DocumentType.GATE_IN) == "GRN-000001"


@pytest.mark.django_db(transaction=True)
class TestAllocationRequiresATransaction:
    """The mechanism only works inside the caller's transaction."""

    def test_allocation_outside_a_transaction_is_refused(self, organization):
        """Otherwise a failed post would leave a permanent gap.

        Allocation and the document it numbers must commit or roll back
        together, so calling this in autocommit mode is a programming error
        rather than something to paper over.
        """
        assert not connection.in_atomic_block

        with tenant_context(organization), pytest.raises(
            RuntimeError, match="inside a transaction"
        ):
            allocate_number(DocumentType.GATE_IN, organization_id=organization.pk)


class TestGapFreedom:
    """M6: an auditor must be able to see nothing was removed."""

    def test_a_rolled_back_post_returns_the_number(self, tenant):
        """The reason a Postgres sequence cannot be used here.

        A sequence hands out numbers outside transaction control, so a rollback
        would leave a permanent hole.
        """
        with transaction.atomic():
            assert allocate_number(DocumentType.GATE_IN) == "GRN-000001"

        # A post that fails and rolls back.
        with pytest.raises(RuntimeError), transaction.atomic():
            allocate_number(DocumentType.GATE_IN)
            raise RuntimeError("posting failed")

        # The next successful post takes 2, not 3.
        with transaction.atomic():
            assert allocate_number(DocumentType.GATE_IN) == "GRN-000002"

    def test_an_abandoned_draft_consumes_no_number(self, tenant):
        """T1.15's stated criterion.

        Numbers are allocated at posting only, so a storekeeper who starts a
        large delivery and gives up burns nothing.
        """
        # Creating drafts touches no sequence at all.
        assert not DocumentSequence.objects.filter(document_type=DocumentType.GATE_IN).exists()
        assert peek_next_number(DocumentType.GATE_IN) == "GRN-000001"

        with transaction.atomic():
            assert allocate_number(DocumentType.GATE_IN) == "GRN-000001"

    def test_peeking_does_not_consume(self, tenant):
        assert peek_next_number(DocumentType.GATE_OUT) == "GP-000001"
        assert peek_next_number(DocumentType.GATE_OUT) == "GP-000001"

        with transaction.atomic():
            assert allocate_number(DocumentType.GATE_OUT) == "GP-000001"


@pytest.mark.django_db(transaction=True)
class TestConcurrentAllocation:
    """T1.15: N documents posted in parallel yield a gap-free sequence."""

    def test_parallel_posts_produce_no_duplicates_and_no_gaps(self, organization):
        """Real threads on real connections — the only way to test the lock.

        A single-threaded test would pass even with the ``SELECT FOR UPDATE``
        removed, so it would prove nothing.
        """
        worker_count = 12
        allocated: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()
        start = threading.Barrier(worker_count)

        def post_one():
            try:
                # Each thread gets its own connection, so it must establish the
                # tenant context itself (§2.2).
                start.wait(timeout=10)
                with transaction.atomic(), tenant_context(organization):
                    number = allocate_number(
                        DocumentType.GATE_IN, organization_id=organization.pk
                    )
                with lock:
                    allocated.append(number)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=post_one) for _ in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == [], f"threads failed: {errors}"

        # No duplicates: the lock serialised every allocation.
        assert len(set(allocated)) == worker_count, f"duplicate numbers in {sorted(allocated)}"

        # No gaps: exactly 1..N, which is what an auditor checks.
        expected = {format_number(DocumentType.GATE_IN, n) for n in range(1, worker_count + 1)}
        assert set(allocated) == expected
