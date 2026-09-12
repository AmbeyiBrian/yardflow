"""Generate the OpenAPI schema (requirement N-10, task T1.21).

A thin wrapper around ``spectacular`` that supplies the tenant context filter
introspection needs (see ``core.schema.schema_generation_context``). Use this
rather than ``manage.py spectacular`` directly:

    python manage.py openapi --file api-schema.yml
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand

from core.schema import schema_generation_context


class Command(BaseCommand):
    help = "Generate the OpenAPI schema, with the tenant context generation needs."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--file", default=None, help="Write to this path.")
        parser.add_argument("--fail-on-warn", action="store_true")

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        arguments = {"validate": True}
        if options.get("file"):
            arguments["file"] = options["file"]
        if options.get("fail_on_warn"):
            arguments["fail_on_warn"] = True

        with schema_generation_context():
            call_command("spectacular", stdout=self.stdout, **arguments)
