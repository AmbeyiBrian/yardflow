"""Tenant-aware Celery base task (design §2.2).

A task has no request and therefore no subdomain to resolve an organization
from. Every tenant-aware task must carry ``organization_id`` explicitly and set
the context itself.

``TenantTask`` **refuses to run** without one, for the same reason
``TenantManager`` refuses to query: the alternative is a task that quietly
operates on the wrong tenant's data, or on everyone's.
"""

from __future__ import annotations

from celery import Task
from django.db import transaction

from core.tenancy import (
    TenantContextMissing,
    activate_organization,
    reset_current_organization,
)


class TenantTask(Task):
    """Base class for any task touching tenant-scoped data.

    Usage::

        @shared_task(base=TenantTask, bind=True)
        def sweep_overdue_custody(self, organization_id):
            ...

    The organization is taken from the ``organization_id`` keyword argument.
    Both the Python context and the Postgres session setting are established, so
    a task gets exactly the same isolation guarantees as a request.
    """

    abstract = True

    #: Tasks that are genuinely cross-tenant — a nightly sweep that fans out one
    #: child task per organization, for instance — set this to opt out.
    requires_organization = True

    def __call__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        organization_id = kwargs.get("organization_id")

        if organization_id is None:
            if self.requires_organization:
                raise TenantContextMissing(
                    f"Task {self.name} requires an explicit organization_id. A task "
                    f"has no request to resolve a tenant from, so it must be told "
                    f"which one it is acting for (§2.2). If this task is genuinely "
                    f"cross-tenant, set requires_organization = False and use "
                    f"all_objects."
                )
            return super().__call__(*args, **kwargs)

        # The Postgres setting is transaction-scoped, so the task body has to run
        # inside a transaction for row-level security to apply to it.
        with transaction.atomic():
            token = activate_organization(organization_id)
            try:
                return super().__call__(*args, **kwargs)
            finally:
                reset_current_organization(token)
