"""A test-only app holding a concrete TenantModel.

The tenancy base classes (§2.1) must be tested against a real, migrated table —
row-level security and the save() guard are not observable on an abstract model.
This app is installed only by ``config.settings.test``.
"""
