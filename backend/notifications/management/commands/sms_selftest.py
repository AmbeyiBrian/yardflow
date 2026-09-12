"""Check the SMS configuration without messaging anybody (T8.11, L1).

The moment a customer hands over an API key the question is "does this work?",
and answering it by sending a real SMS to a real technician is a poor first move —
it costs a credit, it reaches somebody who did not ask for it, and if it fails you
still do not know whether the key, the email or the sender ID was wrong.

So this asks UjumbeSMS for the account balance instead. It exercises the same
credentials, the same headers and the same host as a send, proves the account is
reachable, and reports the credits — which is the other thing worth knowing,
because an account with no credits fails silently for every message.

    python manage.py sms_selftest
    python manage.py sms_selftest --to 0722000000     # sends one real message
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from notifications.channels import get_sms_backend
from notifications.channels.base import RenderedMessage


class Command(BaseCommand):
    help = "Verify the SMS provider configuration, and optionally send one message."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--to",
            default="",
            help=(
                "Send one real message to this number. Costs a credit and "
                "reaches a real phone, so it is opt-in."
            ),
        )
        parser.add_argument(
            "--message",
            default="YardFlow test message. No action needed.",
            help="What to send with --to.",
        )

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        backend = get_sms_backend()
        self.stdout.write(f"SMS_BACKEND: {settings.SMS_BACKEND}")
        self.stdout.write(f"adapter:     {type(backend).__module__}.{type(backend).__name__}")

        # The logging adapter is the development default and sends nothing real,
        # which is worth saying plainly rather than letting somebody conclude the
        # provider is configured when it is not (§12.0).
        if type(backend).__name__ == "LoggingSmsBackend":
            self.stdout.write(
                self.style.WARNING(
                    "\nThis is the development adapter: it prints messages and "
                    "sends nothing.\nSet SMS_BACKEND to "
                    "notifications.channels.ujumbe_sms.UjumbeSmsBackend in "
                    "backend/.env\nto talk to UjumbeSMS."
                )
            )

        missing = getattr(type(backend), "missing_configuration", lambda: [])()
        if missing:
            self.stdout.write(self.style.ERROR(f"\nNot configured — missing: {', '.join(missing)}"))
            self.stdout.write("Set them in backend/.env (see .env.example).")
            return

        if hasattr(backend, "balance"):
            self.stdout.write(f"host:        {backend.endpoint}")
            self.stdout.write("\nAsking the provider for the account balance…")
            result = backend.balance()
            if result.get("ok"):
                meta = result.get("meta") or {}
                credits = result.get("credits")
                self.stdout.write(
                    self.style.SUCCESS(
                        "  credentials accepted. Credits: "
                        + ("unreported" if credits is None else str(credits))
                    )
                )
                # The rate turns credits into messages, which is the number
                # somebody actually plans around.
                rate = meta.get("rate")
                if rate and credits is not None:
                    try:
                        self.stdout.write(
                            f"  rate {rate} per SMS — about "
                            f"{int(float(credits) / float(rate)):,} messages"
                        )
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass
                if meta.get("user"):
                    self.stdout.write(f"  account: {meta['user']}")
            else:
                self.stdout.write(self.style.ERROR(f"  refused: {result.get('error') or result}"))
                # The three things that are actually wrong when this fails, in
                # the order they are usually wrong.
                self.stdout.write(
                    "\n  Check, in this order:\n"
                    "   1. UJUMBE_SMS_API_KEY matches the dashboard exactly\n"
                    "   2. UJUMBE_SMS_ACCOUNT_EMAIL is the account's login email\n"
                    "   3. UJUMBE_SMS_BASE_URL is the host your account was "
                    "issued against"
                )
                return

        if not options["to"]:
            self.stdout.write(
                "\nNo --to given, so nothing was sent. Add --to 07xxxxxxxx to "
                "send one real message."
            )
            return

        self.stdout.write(f"\nSending one message to {options['to']}…")
        outcome = backend.send(
            options["to"], RenderedMessage(subject="YardFlow", body=options["message"])
        )
        if outcome.succeeded:
            self.stdout.write(
                self.style.SUCCESS(
                    f"  sent. Provider reference: {outcome.provider_reference or '—'}"
                )
            )
        else:
            self.stdout.write(self.style.ERROR(f"  failed: {outcome.error}"))
            self.stdout.write(
                f"  retryable: {outcome.retryable} "
                + (
                    "(the sweep will try again)"
                    if outcome.retryable
                    else "(needs a fix, not a retry)"
                )
            )
