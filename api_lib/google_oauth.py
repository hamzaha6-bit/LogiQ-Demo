"""Per-user Google OAuth — Gmail, Sheets, Calendar."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from crypto import decrypt_token_data, encrypt_token_data
from supabase_rest import env, rest_delete, rest_get, rest_patch, rest_post, rest_post_with_error

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
        return decrypt_token_data(rows[0]["token_data"])
    return None


def save_user_token(user_id: str, token_data: dict) -> Tuple[bool, str]:
    row, err = rest_post_with_error(
        "user_integrations",
        {
            "user_id": user_id,
            "integration": "gmail",
            "token_data": encrypt_token_data(token_data),
            "connected_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="user_id,integration",
    )
    return row is not None, err


def disconnect_user_token(user_id: str) -> Tuple[bool, str]:
    return rest_delete(
        "user_integrations",
        {"user_id": user_id, "integration": "gmail"},
    )


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


def get_gmail_service(user_id: str):
    return build("gmail", "v1", credentials=get_credentials(user_id), cache_discovery=False)


def get_sheets_service(user_id: str):
    return build("sheets", "v4", credentials=get_credentials(user_id), cache_discovery=False)


def get_calendar_service(user_id: str):
    return build("calendar", "v3", credentials=get_credentials(user_id), cache_discovery=False)


# ── Gmail read/search/draft/label/thread helpers (Phase 1 Track A) ──────────

def _b64url_decode(data: str) -> str:
    import base64

    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _header(headers: List[Dict[str, str]], name: str) -> str:
    lname = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == lname:
            return h.get("value") or ""
    return ""


def _extract_body(payload: Dict[str, Any]) -> str:
    """Walk a Gmail message payload and return the best-effort plain-text body."""
    if not payload:
        return ""
    mime = payload.get("mimeType") or ""
    body = payload.get("body") or {}
    if mime == "text/plain" and body.get("data"):
        return _b64url_decode(body["data"])

    parts = payload.get("parts") or []
    # Prefer text/plain, fall back to text/html, then recurse.
    for part in parts:
        if (part.get("mimeType") or "") == "text/plain" and (part.get("body") or {}).get("data"):
            return _b64url_decode(part["body"]["data"])
    for part in parts:
        if (part.get("mimeType") or "") == "text/html" and (part.get("body") or {}).get("data"):
            return _b64url_decode(part["body"]["data"])
    for part in parts:
        nested = _extract_body(part)
        if nested:
            return nested
    if body.get("data"):
        return _b64url_decode(body["data"])
    return ""


def build_gmail_query(params: Dict[str, Any]) -> str:
    """Build a Gmail search query string from structured filter params.

    Supported keys: query (freeform), from, to, subject, after, before,
    newer_than, older_than, label, has_attachment, is_unread.
    Dates for after/before accept YYYY/MM/DD or YYYY-MM-DD.
    """
    terms: List[str] = []
    freeform = (params.get("query") or params.get("q") or "").strip()
    if freeform:
        terms.append(freeform)

    def _q(value: str) -> str:
        value = value.strip()
        return f'"{value}"' if " " in value else value

    if params.get("from"):
        terms.append(f"from:{_q(str(params['from']))}")
    if params.get("to"):
        terms.append(f"to:{_q(str(params['to']))}")
    if params.get("subject"):
        terms.append(f"subject:{_q(str(params['subject']))}")
    for key in ("after", "before", "newer_than", "older_than"):
        if params.get(key):
            val = str(params[key]).strip().replace("-", "/")
            terms.append(f"{key}:{val}")
    if params.get("label"):
        terms.append(f"label:{_q(str(params['label']))}")
    if params.get("has_attachment") in (True, "true", "True", 1, "1"):
        terms.append("has:attachment")
    if params.get("is_unread") in (True, "true", "True", 1, "1"):
        terms.append("is:unread")
    return " ".join(terms).strip()


def list_messages(user_id: str, query: str = "", max_results: int = 10) -> Dict[str, Any]:
    service = get_gmail_service(user_id)
    try:
        limit = max(1, min(int(max_results or 10), 100))
    except (TypeError, ValueError):
        limit = 10
    kwargs: Dict[str, Any] = {"userId": "me", "maxResults": limit}
    if query:
        kwargs["q"] = query
    resp = service.users().messages().list(**kwargs).execute()
    messages = resp.get("messages") or []
    return {
        "query": query,
        "message_ids": [m.get("id") for m in messages],
        "messages": messages,
        "count": len(messages),
        "result_size_estimate": resp.get("resultSizeEstimate", len(messages)),
    }


def search_messages(user_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    query = build_gmail_query(params)
    if not query:
        raise ValueError("search requires at least one filter (query, from, subject, after, ...)")
    max_results = params.get("max_results") or params.get("maxResults") or 10
    result = list_messages(user_id, query=query, max_results=max_results)
    result["built_query"] = query
    return result


def read_message(user_id: str, message_id: str) -> Dict[str, Any]:
    mid = (message_id or "").strip()
    if not mid:
        raise ValueError("message_id is required")
    service = get_gmail_service(user_id)
    msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    return {
        "message_id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "subject": _header(headers, "Subject"),
        "from": _header(headers, "From"),
        "to": _header(headers, "To"),
        "date": _header(headers, "Date"),
        "snippet": msg.get("snippet", ""),
        "body": _extract_body(payload),
        "label_ids": msg.get("labelIds") or [],
    }


def create_draft(user_id: str, to: str, subject: str, body: str, from_name: str = "") -> Dict[str, Any]:
    import base64
    from email.mime.text import MIMEText

    creds = get_credentials(user_id)
    health = check_gmail_health(user_id)
    sender = health.get("email") or env("GMAIL_SENDER_EMAIL") or "me"
    from_header = f'"{from_name}" <{sender}>' if from_name else sender
    message = MIMEText(body or "", "plain", "utf-8")
    message["to"] = to
    message["from"] = from_header
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return {
        "draft_id": draft.get("id"),
        "message_id": (draft.get("message") or {}).get("id"),
        "to": to,
        "subject": subject,
        "created": True,
    }


def _resolve_label_ids(service, names: List[str], create_missing: bool = False) -> List[str]:
    if not names:
        return []
    system = {
        "inbox": "INBOX", "unread": "UNREAD", "starred": "STARRED",
        "important": "IMPORTANT", "sent": "SENT", "draft": "DRAFT",
        "spam": "SPAM", "trash": "TRASH",
    }
    existing = service.users().labels().list(userId="me").execute().get("labels") or []
    by_name = {(l.get("name") or "").lower(): l.get("id") for l in existing}
    ids: List[str] = []
    for name in names:
        clean = (name or "").strip()
        if not clean:
            continue
        low = clean.lower()
        if low in system:
            ids.append(system[low])
        elif low in by_name:
            ids.append(by_name[low])
        elif create_missing:
            created = service.users().labels().create(
                userId="me",
                body={"name": clean, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
            ).execute()
            ids.append(created.get("id"))
            by_name[low] = created.get("id")
        else:
            raise ValueError(f"Label not found: {clean}")
    return ids


def modify_labels(
    user_id: str,
    message_id: str,
    add_labels: Optional[List[str]] = None,
    remove_labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    mid = (message_id or "").strip()
    if not mid:
        raise ValueError("message_id is required")
    if not add_labels and not remove_labels:
        raise ValueError("provide add_labels and/or remove_labels")
    service = get_gmail_service(user_id)
    add_ids = _resolve_label_ids(service, add_labels or [], create_missing=True)
    remove_ids = _resolve_label_ids(service, remove_labels or [], create_missing=False)
    updated = service.users().messages().modify(
        userId="me",
        id=mid,
        body={"addLabelIds": add_ids, "removeLabelIds": remove_ids},
    ).execute()
    return {
        "message_id": updated.get("id"),
        "label_ids": updated.get("labelIds") or [],
        "added": add_ids,
        "removed": remove_ids,
        "modified": True,
    }


def get_thread(user_id: str, thread_id: str) -> Dict[str, Any]:
    tid = (thread_id or "").strip()
    if not tid:
        raise ValueError("thread_id is required")
    service = get_gmail_service(user_id)
    thread = service.users().threads().get(userId="me", id=tid, format="full").execute()
    messages = []
    for msg in thread.get("messages") or []:
        payload = msg.get("payload") or {}
        headers = payload.get("headers") or []
        messages.append({
            "message_id": msg.get("id"),
            "subject": _header(headers, "Subject"),
            "from": _header(headers, "From"),
            "date": _header(headers, "Date"),
            "snippet": msg.get("snippet", ""),
            "body": _extract_body(payload),
        })
    return {"thread_id": thread.get("id"), "messages": messages, "count": len(messages)}
