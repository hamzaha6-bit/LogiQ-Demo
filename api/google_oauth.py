"""Per-user Google OAuth — Gmail, Sheets, Calendar."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from supabase_rest import env, rest_get, rest_patch, rest_post

GMAIL_REDIRECT_URI = env("GMAIL_REDIRECT_URI") or "https://app.logiqops.co.uk/api/auth/gmail/callback"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def parse_client_config() -> dict:
    raw = env("GMAIL_CREDENTIALS_JSON")
    if not raw:
        raise ValueError("GMAIL_CREDENTIALS_JSON not set")
    config = json.loads(raw)
    section = config.get("web") or config.get("installed")
    if not section:
        raise ValueError("GMAIL_CREDENTIALS_JSON must contain web or installed")
    section_name = "web" if config.get("web") else "installed"
    section["redirect_uris"] = [GMAIL_REDIRECT_URI]
    config[section_name] = section
    return config


def is_oauth_configured() -> bool:
    try:
        return bool(env("GMAIL_CREDENTIALS_JSON") and parse_client_config())
    except Exception:
        return False


def load_user_token(user_id: str) -> Optional[dict]:
    rows = rest_get(
        "user_integrations",
        {
            "user_id": f"eq.{user_id}",
            "integration": "eq.gmail",
            "select": "token_data,connected_at",
            "limit": "1",
        },
    )
    if rows and rows[0].get("token_data"):
        return rows[0]["token_data"]
    return None


def save_user_token(user_id: str, token_data: dict) -> Tuple[bool, str]:
    row, err = rest_post_with_error(
        "user_integrations",
        {
            "user_id": user_id,
            "integration": "gmail",
            "token_data": token_data,
            "connected_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="user_id,integration",
    )
    return row is not None, err


def _scopes_in_token(token_data: dict) -> List[str]:
    scopes = token_data.get("scopes") or []
    if isinstance(scopes, str):
        return [s.strip() for s in scopes.split(",") if s.strip()]
    return list(scopes)


def has_scope(user_id: str, scope: str) -> bool:
    token_data = load_user_token(user_id)
    if not token_data:
        return False
    scopes = _scopes_in_token(token_data)
    return scope in scopes or any(scope in s for s in scopes)


def get_credentials(user_id: str) -> Credentials:
    token_data = load_user_token(user_id)
    if not token_data:
        raise PermissionError("Connect your Google account — visit /api/auth/gmail/connect")
    creds = Credentials.from_authorized_user_info(token_data, GOOGLE_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_user_token(user_id, json.loads(creds.to_json()))
    if not creds.valid:
        raise PermissionError("Google token expired — reconnect at /api/auth/gmail/connect")
    return creds


def check_gmail_health(user_id: str) -> Dict[str, Any]:
    """Validate token, refresh if needed, probe Gmail profile."""
    result: Dict[str, Any] = {
        "connected": False,
        "healthy": False,
        "email": "",
        "sheets_scope": False,
        "calendar_scope": False,
        "error": "",
    }
    token_data = load_user_token(user_id)
    if not token_data:
        result["error"] = "not_connected"
        return result
    result["connected"] = True
    result["sheets_scope"] = has_scope(user_id, SHEETS_SCOPE) or has_scope(
        user_id, "spreadsheets.readonly"
    )
    result["calendar_scope"] = has_scope(user_id, CALENDAR_SCOPE) or has_scope(
        user_id, "calendar.readonly"
    )
    try:
        creds = get_credentials(user_id)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
        result["email"] = profile.get("emailAddress", "")
        result["healthy"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def send_user_email(
    user_id: str,
    to: str,
    subject: str,
    body: str,
    from_name: str = "",
) -> Tuple[bool, str]:
    import base64
    from email.mime.text import MIMEText

    creds = get_credentials(user_id)
    health = check_gmail_health(user_id)
    sender = health.get("email") or env("GMAIL_SENDER_EMAIL") or "me"
    from_header = f'"{from_name}" <{sender}>' if from_name else sender
    message = MIMEText(body, "plain", "utf-8")
    message["to"] = to
    message["from"] = from_header
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return True, sent.get("id", "sent")


def get_sheets_service(user_id: str):
    return build("sheets", "v4", credentials=get_credentials(user_id), cache_discovery=False)


def get_calendar_service(user_id: str):
    return build("calendar", "v3", credentials=get_credentials(user_id), cache_discovery=False)
