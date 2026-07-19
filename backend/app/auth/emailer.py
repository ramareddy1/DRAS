"""SMTP sender for login codes. Env-gated; absent config -> 503 at the route."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip())


def send_code(to_email: str, code: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"Your ReconOps sign-in code: {code}"
    msg["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "reconops@localhost"))
    msg["To"] = to_email
    msg.set_content(
        f"Your ReconOps sign-in code is: {code}\n\n"
        "It expires in 10 minutes. If you didn't request it, ignore this email."
    )
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as s:
        s.starttls()
        user, pw = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")
        if user:
            s.login(user, pw)
        s.send_message(msg)
