"""Tenant context and scoped model base classes.

Design §2.1 (layer 1 of four). Requirement A3, decision D2: shared schema with
an organization foreign key, enforced at the query layer *and* by Postgres
row-level security.

The guiding principle: **an unscoped read is a crash in development, never a
silent leak in production.** If no organization is in context,
``TenantManager`` raises rather than returning every tenant's rows.

The four layers, so that one forgotten filter cannot leak data:

1. this module — model manager and ``save()`` guard
2. ``core.middleware.TenantMiddleware`` — resolves the organization per request (T1.5)
3. Postgres row-level security — a second, independent barrier (T1.6)
4. ``core.api.TenantScopedViewSet`` — returns 404, never 403, cross-tenant (T1.8)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from django.db import models

#: Anything that can identify an organization: the model, its UUID, or None.
Organizationish = object


class TenantContextMissing(RuntimeError):
    """Raised when tenant-scoped data is touched with no organization in context.

    This is deliberately loud. The alternative — returning an unfiltered
    queryset — is a cross-tenant data leak (A3). If you genuinely need
    cross-tenant access you are writing platform admin code, and you must say so
    explicitly by using ``all_objects``.
    """


# The active organization for the current request, task or block. A ContextVar
# rather than thread-local state so it behaves correctly under ASGI and inside
# async views.
_current_organization_id: ContextVar[uuid.UUID | None] = ContextVar(
    "yardflow_current_organization_id", default=None
)


def get_current_organization_id() -> uuid.UUID | None:
    """Return the organization in context, or ``None`` if there is none."""
    return _current_organization_id.get()


def require_current_organization_id() -> uuid.UUID:
    """Return the organization in context, raising if there is none."""
    organization_id = _current_organization_id.get()
    if organization_id is None:
        raise TenantContextMissing(
            "No organization is in context. Tenant-scoped models cannot be "
            "queried without one. Wrap the call in `tenant_context(org)`, or — "
            "if this is deliberately cross-tenant platform admin code — use "
            "`Model.all_objects` and say so."
        )
    return organization_id


def set_current_organization_id(organization_id: uuid.UUID | None) -> Token:
    """Set the active organization, returning a token for restoring the previous one."""
    return _current_organization_id.set(organization_id)


def reset_current_organization(token: Token) -> None:
    """Restore the organization that was active before ``token`` was issued."""
    _current_organization_id.reset(token)


@contextmanager
def tenant_context(organization: Organizationish) -> Iterator[None]:
    """Run a block with ``organization`` active, in Python and in Postgres.

    Accepts an ``Organization``, a UUID or ``None``. Always restores the previous
    value, so nesting is safe.

    The database setting is published only when a transaction is already open.
    Outside one, a transaction-local setting would be discarded immediately, and
    setting it non-locally would leak into the next request through a pooled
    connection (§2.2). Requests and Celery tasks both open a transaction before
    activating a tenant, so in practice it is always published where it matters.
    """
    from django.db import connection

    organization_id: uuid.UUID | None = getattr(organization, "pk", organization)  # type: ignore[assignment,arg-type]
    token = set_current_organization_id(organization_id)

    published = False
    previous_setting = ""
    if connection.in_atomic_block:
        previous_setting = get_database_organization()
        set_database_organization(organization_id)
        published = True

    try:
        yield
    finally:
        if published:
            _set_organization_setting(previous_setting)
        reset_current_organization(token)


class TenantQuerySet(models.QuerySet):
    """Base queryset for tenant-scoped models.

    Subclass this to add model-specific query methods, then build the manager
    with ``TenantManager.from_queryset(YourQuerySet)``. Doing it that way keeps
    the organization filter *and* leaves ``all_objects`` inherited, so no model
    has to re-declare the escape hatch — which is what keeps the grep in T1.20
    meaningful.
    """


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):  # type: ignore[misc]
    """Scopes every query to the organization in context.

    Raises :class:`TenantContextMissing` when there is none, rather than
    returning rows belonging to everyone (§2.1).
    """

    # Django uses this for reverse relations and cascade collection; keeping it
    # False means related managers are scoped too.
    use_in_migrations = False

    def get_queryset(self) -> TenantQuerySet:
        organization_id = require_current_organization_id()
        return super().get_queryset().filter(organization_id=organization_id)

    def none(self) -> TenantQuerySet:
        """An empty queryset, which needs no tenant.

        DRF and the OpenAPI generator ask managers for ``.none()`` outside any
        request, to learn a model without touching data (N-10). Refusing would
        break that tooling in order to guard a queryset that by definition
        returns nothing — so this deliberately skips the organization filter
        rather than the other way round.
        """
        return self._queryset_class(
            model=self.model, using=self._db, hints=self._hints
        ).none()


class TenantModel(models.Model):
    """Base class for every model owned by a tenant (§2.1).

    Subclasses get:

    * an ``organization`` foreign key, stamped automatically on save
    * ``objects`` — scoped to the organization in context
    * ``all_objects`` — unscoped, and the *only* way to cross tenants

    ``all_objects`` is deliberately grep-able. CI fails the build when it appears
    outside ``platform_admin/`` or a migration (T1.20).
    """

    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.PROTECT,
        db_index=True,
        editable=False,
        related_name="%(app_label)s_%(class)s_set",
    )

    # Order matters: the first manager declared becomes _default_manager, which
    # is what admin, model forms and reverse relations reach for. That must be
    # the scoped one.
    objects = TenantManager()
    # The declaration order here is load bearing (see the comment above), so
    # the Django style guide's field-before-manager rule is suppressed.
    all_objects = models.Manager()  # noqa: DJ012

    class Meta:
        abstract = True
        default_manager_name = "objects"
        # Django's cascade collector and related descriptors use _base_manager.
        # It stays unscoped on purpose: a delete must be able to see the rows it
        # is about to cascade to. Row-level security (§2.3) is the barrier at
        # that level, which is exactly why the design has four layers and not
        # one.
        base_manager_name = "all_objects"

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Stamp the organization from context, and refuse cross-tenant writes.

        Two distinct protections:

        * a new row with no organization set inherits the one in context, so
          callers cannot forget it
        * a row whose organization differs from the active context is refused,
          so a leaked object from another tenant cannot be written to
        """
        context_organization_id = get_current_organization_id()

        if self.organization_id is None:
            if context_organization_id is None:
                raise TenantContextMissing(
                    f"Cannot save {type(self).__name__} without an organization: "
                    f"none was set on the instance and none is in context."
                )
            self.organization_id = context_organization_id
        elif context_organization_id is not None and str(self.organization_id) != str(
            context_organization_id
        ):
            # Compared as strings deliberately. An organization id is a UUID on a
            # model instance but arrives as a string from a Celery task argument
            # or a JWT claim, and `UUID(...) != "same-uuid"` is true — so the
            # guard used to refuse a write to *the very organization in context*,
            # with a message naming the same id twice. Found by a task that
            # stores a file; it would have broken every scheduled sweep that
            # writes (§2.2).
            raise TenantContextMissing(
                f"Refusing to save {type(self).__name__} belonging to "
                f"organization {self.organization_id} while organization "
                f"{context_organization_id} is in context. This is a "
                f"cross-tenant write (A3)."
            )

        return super().save(*args, **kwargs)


# --------------------------------------------------------------------------
# Layer 3 handshake — the Postgres session variable
# --------------------------------------------------------------------------

# Name of the Postgres setting every row-level security policy reads (§2.3).
DB_ORGANIZATION_SETTING = "app.current_org"


def set_database_organization(organization_id: uuid.UUID | None) -> None:
    """Publish the active organization to Postgres for row-level security.

    Uses ``set_config(name, value, is_local => true)`` rather than
    ``SET LOCAL``, for two reasons:

    * ``SET`` cannot take a bound parameter, so the value would have to be
      interpolated into SQL. ``set_config`` takes it as a parameter, which
      removes the injection question entirely.
    * ``is_local => true`` makes the setting transaction-scoped, exactly like
      ``SET LOCAL``. A pooled connection therefore cannot leak it into the next
      request (§2.2).

    **Must be called inside a transaction.** Outside one, a local setting is
    discarded immediately and every policy would see an empty organization.
    """
    _set_organization_setting(str(organization_id) if organization_id else "")


def _set_organization_setting(value: str) -> None:
    """Write the raw session setting. Transaction-scoped, so it cannot leak."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, true)", [DB_ORGANIZATION_SETTING, value])


def get_database_organization() -> str:
    """Return the organization Postgres currently has in session, for assertions."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [DB_ORGANIZATION_SETTING])
        row = cursor.fetchone()
    return (row[0] or "") if row else ""


def activate_organization(organization_id: uuid.UUID | None) -> Token:
    """Activate an organization in both Python and Postgres.

    Returns the token needed to restore the previous Python context. The
    database setting needs no restoring: it dies with the transaction.
    """
    token = set_current_organization_id(organization_id)
    set_database_organization(organization_id)
    return token
