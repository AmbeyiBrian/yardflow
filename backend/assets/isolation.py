"""Isolation fixtures for the asset register (T17.2, A3).

The register has no endpoints yet (T17.4 adds them and registers each
basename here then). Until then the tables are covered at the database by
RLS, which ``assets/tests/test_models.py`` exercises with raw SQL.
"""


def register() -> None:
    return None
