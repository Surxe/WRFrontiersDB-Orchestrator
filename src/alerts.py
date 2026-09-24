"""Email delivery for the run report.

Modelled on steam-price-tracker's alerter, but the orchestrator is independent:
its own option names (``smtp_host`` / ``smtp_port`` / ``smtp_user`` /
``smtp_password`` / ``email_to``), resolved from the orchestrator's own config.
The live values are supplied by the orchestrator's non-checked-in secrets (the
same Gmail account happens to back the price tracker, but nothing is shared in
code and no credential file is created here).

One email per run (no per-item dedup): the tracker's daily-dedup state store has
no place here. Delivery never crashes the run — every SMTP/OS error is caught and
logged, because the report is the tail end of an already-finished pipeline.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Optional

from loguru import logger

from report import RunReport


@dataclass(frozen=True)
class EmailConfig:
    """SMTP settings for the report email. The password is a Gmail App Password."""

    host: str
    port: int
    user: str          # authenticated account; also the From address
    password: str
    to_addr: str

    @classmethod
    def from_options(cls, options) -> Optional["EmailConfig"]:
        """Build from resolved options, or ``None`` if email is disabled.

        Email is on only when user, password, and recipient are all present —
        exactly the tracker's gate. Absent any, the report is logged, not sent.
        """
        user = getattr(options, "smtp_user", None)
        password = getattr(options, "smtp_password", None)
        to_addr = getattr(options, "email_to", None)
        if not user or not password or not to_addr:
            return None
        return cls(
            host=getattr(options, "smtp_host", "smtp.gmail.com"),
            port=int(getattr(options, "smtp_port", 587)),
            user=user,
            password=password,
            to_addr=to_addr,
        )


class EmailAlerter:
    """Sends one run-report email via SMTP (STARTTLS + login).

    ``smtp_factory`` is injected so compose/auth is testable without a network.
    """

    def __init__(
        self,
        email_config: EmailConfig,
        smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
    ) -> None:
        self.config = email_config
        self.smtp_factory = smtp_factory

    def send(self, report: RunReport) -> bool:
        """Send the report; return True only if the email was delivered."""
        message = self._compose(report)
        try:
            self._deliver(message)
        except smtplib.SMTPAuthenticationError as exc:
            logger.error(
                "Run-report email failed: SMTP authentication rejected. Check the "
                f"App Password and 2-Step Verification for {self.config.user}. ({exc})"
            )
            return False
        except (smtplib.SMTPException, OSError) as exc:
            logger.error(f"Run-report email failed: {exc}")
            return False
        logger.info(f"Run report emailed to {self.config.to_addr}.")
        return True

    def _compose(self, report: RunReport) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = report.subject()
        message["From"] = self.config.user
        message["To"] = self.config.to_addr
        # Plain text is the fallback; the HTML alternative hyperlinks the file://
        # log links (plain-text clients don't auto-link file:// URIs).
        message.set_content(report.body())
        message.add_alternative(report.body_html(), subtype="html")
        return message

    def _deliver(self, message: EmailMessage) -> None:
        with self.smtp_factory(self.config.host, self.config.port) as server:
            server.starttls()
            server.login(self.config.user, self.config.password)
            server.send_message(message)


def send_report(options, report: RunReport) -> bool:
    """Email the report if email is configured; otherwise log it and return False."""
    config = EmailConfig.from_options(options)
    if config is None:
        logger.info(
            "Email not configured (SMTP_USER / SMTP_PASSWORD / EMAIL_TO); "
            "run report logged only:\n" + report.body()
        )
        return False
    return EmailAlerter(config).send(report)
