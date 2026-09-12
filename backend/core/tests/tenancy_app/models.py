"""A minimal concrete tenant-scoped model, for exercising §2.1 and §2.3."""

from django.db import models

from core.tenancy import TenantModel


class Widget(TenantModel):
    """Stands in for any tenant-owned record."""

    name = models.CharField(max_length=100)

    def __str__(self) -> str:
        return self.name
