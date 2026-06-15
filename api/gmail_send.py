"""Platform Gmail send helper for Vercel standalone API functions."""
from __future__ import annotations

import base64
import json
import os
from email.mime.text import MIMEText
from typing import Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _env(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def is_gmail_configured() -> bool:
    return bool(_env("GMAIL_SENDER_EMAIL") and _env("GMAIL_TOKEN_JSON"))


def _load_credentials() -> Credentials:
    token_raw = _env("GMAIL_TOKEN_JSON")
    if not token_raw:
        raise ValueError("GMAIL_TOKEN_JSON not configured")
    token_data = json.loads(token_raw)
    creds = Credentials.from_authorized_user_info(token_data, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise ValueError("Gmail token invalid — re-authorise the platform sender account")
    return creds


def send_platform_email(
    to: str,
    subject: str,
    body: str,
    from_name: str = "",
) -> Tuple[bool, str]:
    sender = _env("GMAIL_SENDER_EMAIL") or "hamza@logiq.org.uk"
    creds = _load_credentials()
    from_header = f'"{from_name}" <{sender}>' if from_name else sender

    message = MIMEText(body, "plain", "utf-8")
    message["to"] = to
    message["from"] = from_header
    message["subject"] = subject
    message["Reply-To"] = sender

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return True, result.get("id", "sent")
