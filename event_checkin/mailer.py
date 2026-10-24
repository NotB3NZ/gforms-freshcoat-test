import smtplib
import ssl
from email.message import EmailMessage


class EmailNotSent(RuntimeError):
    """Failure known to precede acceptance by SMTP. Explicit retry is safe."""


class EmailAmbiguous(RuntimeError):
    """SMTP may have accepted the message. Staff must review before resending."""


class SMTPMailer:
    def __init__(self, settings):
        self.settings = settings

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
        s.require_email()
        try:
            message = self.build_message(recipient, name, participant_id, card_path, message_id, ticket_tier=ticket_tier)
        except EmailNotSent:
            raise
        except Exception:
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
        except Exception:
            if smtp:
                smtp.close()
            raise EmailNotSent("SMTP connection, TLS or authentication failed; no message submitted.") from None
        try:
            smtp.send_message(message)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise EmailNotSent("SMTP explicitly rejected the message; correct settings/address then retry.") from None
        except Exception:
            raise EmailAmbiguous("SMTP result is uncertain; check Sent/provider logs before resolving.") from None
        finally:
            # A QUIT failure after a successful DATA response must not misclassify delivery.
            smtp.close()
