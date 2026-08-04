"""Gmail SMTP sender — importable library + CLI test.

Library usage (from another script):
    import sys; sys.path.append(r"C:\\Users\\Dor\\Desktop\\TRADEIN\\mail")
    from send_email import send_report_email
    ok = send_report_email("Subject", "Body text", attachments=["report.md"])

CLI test:
    python send_email.py          # sends a short test email

Setup:
    1. Install the dependency:
           pip install python-dotenv
    2. Ensure a `.env` file exists next to THIS script with:
           SENDER_EMAIL=<gmail address>
           SENDER_APP_PASSWORD=<gmail app password>
           RECIPIENT_EMAIL=<recipient address>

       Note: Gmail requires an App Password (Google Account ->
       Security -> 2-Step Verification -> App passwords). A regular
       account password will be rejected with an authentication error.
"""

import logging
import mimetypes
import os
import smtplib
import socket
import sys
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """Load and validate SMTP credentials from the .env next to this script.

    Anchoring to Path(__file__) (instead of the caller's cwd) means importing
    this module from another directory still finds the right credentials.
    """
    load_dotenv(Path(__file__).resolve().parent / ".env")

    config = {
        "sender": os.environ.get("SENDER_EMAIL"),
        "password": os.environ.get("SENDER_APP_PASSWORD"),
        "recipient": os.environ.get("RECIPIENT_EMAIL"),
    }

    missing = [key for key, value in config.items() if not value]
    if missing:
        raise ValueError(f"Missing required configuration: {', '.join(missing)}")

    return config


def build_message(sender: str, recipient: str, subject: str, body: str,
                  attachments: list[str] | None = None) -> EmailMessage:
    """Build an email message with optional file attachments."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    for path in attachments or []:
        p = Path(path)
        mime, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (mime or "application/octet-stream").split("/", 1)
        message.add_attachment(
            p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name
        )
    return message


def send_email(config: dict, message: EmailMessage) -> bool:
    """Connect to Gmail SMTP over STARTTLS, authenticate, and send the message.

    Returns True on success, False on any failure. The connection is
    always closed in the finally block.
    """
    server = None
    try:
        logger.info("Connecting to %s:%s ...", SMTP_HOST, SMTP_PORT)
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        server.ehlo()
        server.starttls()
        server.ehlo()

        logger.info("Authenticating as %s ...", config["sender"])
        server.login(config["sender"], config["password"])

        logger.info("Sending email to %s ...", config["recipient"])
        server.send_message(message)

        logger.info("Email sent successfully.")
        return True

    except smtplib.SMTPAuthenticationError as exc:
        logger.error(
            "Authentication failed (%s). Check SENDER_EMAIL and "
            "SENDER_APP_PASSWORD — Gmail requires an App Password, "
            "not the regular account password.",
            exc,
        )
    except smtplib.SMTPException as exc:
        logger.error("SMTP error while sending email: %s", exc)
    except socket.error as exc:
        logger.error("Network error connecting to %s:%s — %s", SMTP_HOST, SMTP_PORT, exc)
    finally:
        if server is not None:
            try:
                server.quit()
                logger.info("SMTP connection closed.")
            except (smtplib.SMTPException, socket.error):
                pass

    return False


def send_report_email(subject: str, body: str, attachments: list[str] | None = None) -> bool:
    """Convenience one-call API for other scripts: load config, build, send."""
    try:
        config = load_config()
    except ValueError as exc:
        logger.error("%s", exc)
        return False
    message = build_message(config["sender"], config["recipient"], subject, body, attachments)
    return send_email(config, message)


def main() -> int:
    ok = send_report_email("Test Email from Claude Code", "test")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
