from django.apps import AppConfig


class CommercialsConfig(AppConfig):
    """Expenses, snapshots and the costing engine (§1.2, §4.14).

    Holds what is genuinely new in Epic O. ``Project`` stays in ``network``
    because it *is* the work-order layer renamed (D20), and ``JobLabour`` stays
    in ``jobs`` because it is written from a closeout and has no meaning apart
    from one. What lives here reads across the ledger, the closeouts and the
    expectations without owning any of them.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "commercials"

    def ready(self) -> None:
        from commercials.services import seed_expense_categories
        from core.provisioning import register_tenant_seeder

        # O16: a new tenant arrives with somewhere to file an expense. Empty
        # looks like a feature that does nothing, and the first person to record
        # one should not have to invent a taxonomy first — the same reasoning
        # that seeds a starter approval rule (§5.2).
        register_tenant_seeder(
            seed_expense_categories, order=30, name="seed_expense_categories"
        )

        from commercials import isolation

        isolation.register()
