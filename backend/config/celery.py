"""Celery application (design §1.1, §9.1).

Async work: notification delivery, exports, overdue sweeps, escalation and
expiry, and the nightly ledger verification.

Tenancy note (§2.2): a Celery task has no request and therefore no subdomain to
resolve an organization from. Every tenant-aware task must carry
``organization_id`` explicitly and set the tenant context itself — the task base
class that enforces this arrives in T1.5.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("yardflow")

# All Celery settings live in Django settings under the CELERY_ namespace.
app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()

# The scheduled sweeps live in `core/sweeps.py`, not `tasks.py`, so the default
# discovery never imported them. Beat schedules a task by *name* and needs no
# import, so it happily fired `core.sweeps.retry_notifications` every fifteen
# minutes — and the worker, which does need the import to know the task, logged
# "unregistered task" each time and did nothing. Notification retries and the
# nightly ledger verification had never actually run.
app.autodiscover_tasks(related_name="sweeps")
