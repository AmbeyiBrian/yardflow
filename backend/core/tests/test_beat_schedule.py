"""Every scheduled task is one the worker can actually run (§9.1).

Beat schedules a task by name and needs no import. The worker needs the module
imported to register the task. Those two facts let a schedule fire forever
against a task nobody has loaded — which is exactly what happened: the sweeps
live in `core/sweeps.py`, autodiscovery looks only at `tasks.py`, and every
fifteen minutes the worker logged "unregistered task" and did nothing.
"""

import pytest
from django.conf import settings

from config.celery import app

# Celery's Django fixup runs the system checks on import, and those open a
# database connection. Not a database test; the mark only lets that through.
pytestmark = pytest.mark.django_db


def test_every_scheduled_task_is_registered():
    # Force discovery the way a worker does at start-up.
    app.loader.import_default_modules()

    scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
    registered = set(app.tasks.keys())

    missing = scheduled - registered
    assert not missing, (
        f"beat will fire these and the worker will refuse them: {sorted(missing)}"
    )


def test_the_sweeps_are_among_them():
    """The specific ones that were silently never running."""
    app.loader.import_default_modules()

    for name in (
        "core.sweeps.dispatch_sweeps",
        "core.sweeps.retry_notifications",
        "core.sweeps.verify_ledgers",
    ):
        assert name in app.tasks, name
