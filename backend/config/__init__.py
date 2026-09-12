"""Project package.

Importing the Celery app here ensures ``@shared_task`` is bound to it whenever
Django starts (design §1.1).
"""

from config.celery import app as celery_app

__all__ = ("celery_app",)
