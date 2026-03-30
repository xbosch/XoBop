from __future__ import annotations

import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def smtp_send_email(gmail_user: str, gmail_app_password: str, to_addr: str, subject: str, body: str, attachment_path: str | None = None) -> str:
    if not gmail_user or not gmail_app_password:
        raise RuntimeError("Missing Gmail credentials")

    msg = EmailMessage()
    msg["From"] = gmail_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    if attachment_path:
        p = Path(attachment_path)
        if not p.exists():
            raise RuntimeError(f"Attachment not found: {p}")
        ctype, _ = mimetypes.guess_type(str(p))
        if not ctype:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with p.open("rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=p.name)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(gmail_user, gmail_app_password)
        s.send_message(msg)

    if attachment_path:
        return f"EMAIL_SENT {to_addr} {Path(attachment_path).name}"
    return f"EMAIL_SENT {to_addr}"
