import smtplib
import ssl
from email.message import EmailMessage

from .config import ConfigurationError


class EmailNotSent(RuntimeError):
    """Failure known to precede acceptance by SMTP. Explicit retry is safe."""


class EmailAmbiguous(RuntimeError):
    """SMTP may have accepted the message. Staff must review before resending."""


class SMTPMailer:
    def __init__(self, settings, before_submit=None, diagnostic_sink=None):
        self.settings = settings
        self.before_submit = before_submit
        self.diagnostic_sink = diagnostic_sink

    def _diagnose(self, stage, error=None):
        if self.diagnostic_sink:
            try:
                self.diagnostic_sink(stage, error)
            except Exception:
                pass  # A diagnostic failure must never change delivery classification.

    @staticmethod
    def _close(smtp):
        try:
            smtp.close()
        except Exception:
            pass  # Preserve the DATA result, including an already accepted message.

    def build_message(self, recipient, name, participant_id, card_path, message_id, *, ticket_tier=None):
        """Compose offline; old test callers retain their plain-text message."""
        s = self.settings
        if s.profile == "production" or ticket_tier is not None:
            from .confirmation_email import build_confirmation_message
            return build_confirmation_message(recipient, name, participant_id, card_path, message_id,
                                              ticket_tier=ticket_tier, sender=s.smtp_from or None)
        message = EmailMessage()
        message["From"] = s.smtp_from
        message["To"] = recipient
        message["Subject"] = "Your event registration card"
        message["Message-ID"] = message_id
        message.set_content(
            "Hello " + name + ",\n\nYour registration card is attached. Bring it to the event. "
            "Keep the QR private: it admits one person.\nParticipant ID: " + participant_id + "\n"
        )
        message.add_attachment(card_path.read_bytes(), maintype="image", subtype="png", filename="event-card.png")
        return message

    def send(self, recipient, name, participant_id, card_path, message_id, *, ticket_tier=None):
        s = self.settings
        if not s.email_enabled:
            raise ConfigurationError("EMAIL_ENABLED=false prohibits SMTP sending.")
        s.require_email()
        try:
            message = self.build_message(recipient, name, participant_id, card_path, message_id, ticket_tier=ticket_tier)
        except EmailNotSent as exc:
            self._diagnose("single_entry_message_build", exc)
            raise
        except Exception as exc:
            self._diagnose("single_entry_message_build", exc)
            raise EmailNotSent("Local message construction failed; no message submitted.") from None
        smtp = None
        try:
            context = ssl.create_default_context()
            if s.smtp_security == "ssl":
                smtp = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30, context=context)
            else:
                smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(s.smtp_user, s.smtp_password)
        except Exception as exc:
            self._diagnose("single_entry_smtp_connect", exc)
            if smtp:
                self._close(smtp)
            raise EmailNotSent("SMTP connection, TLS or authentication failed; no message submitted.") from None
        # Recheck after authentication and before DATA. A refusal here is known
        # unsent, including disabling email while TLS/authentication was running.
        try:
            if not s.email_enabled:
                raise ConfigurationError("EMAIL_ENABLED=false prohibits SMTP sending.")
            s.require_email()
            if self.before_submit:
                self.before_submit(message, recipient, name, participant_id, card_path, message_id, ticket_tier)
        except Exception as exc:
            self._diagnose("single_entry_smtp_before_submit", exc)
            self._close(smtp)
            raise EmailNotSent("Final email authorization or registration check failed; no message submitted.") from None
        try:
            smtp.send_message(message)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            self._diagnose("single_entry_smtp_submit", exc)
            raise EmailNotSent("SMTP explicitly rejected the message; correct settings/address then retry.") from None
        except Exception as exc:
            self._diagnose("single_entry_smtp_submit", exc)
            raise EmailAmbiguous("SMTP result is uncertain; check Sent/provider logs before resolving.") from None
        else:
            self._diagnose("single_entry_smtp_accepted")
        finally:
            # A QUIT failure after a successful DATA response must not misclassify delivery.
            self._close(smtp)
