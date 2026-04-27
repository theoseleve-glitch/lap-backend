"""T094 — minimal email sender abstraction.

A `Callable[[str, str, str], None]`-style interface keeps the
Celery wiring testable without committing to a specific provider
yet. The default `make_email_sender()` logs the message at info
level so production can grep audit logs while a real
SMTP/Sendgrid integration lands in a follow-up task. Tests pass
their own recorder callable.

Why the protocol shape (to, subject, body) is plain str instead
of a dataclass: matches the cheapest possible swap-in for any
provider library (smtplib, sendgrid, postmark) without a custom
adapter layer. If structured fields land later (CC, attachments)
we add a richer interface alongside, not in place of.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

logger = structlog.get_logger()

EmailSender = Callable[[str, str, str], None]


def make_email_sender() -> EmailSender:
    """Production no-op sender. Logs to-address + subject so an
    operator can see "we would have emailed X about Y" until the
    real provider is wired."""

    def _send(to: str, subject: str, _body: str) -> None:
        logger.info("email.would_send", to=to, subject=subject)

    return _send
