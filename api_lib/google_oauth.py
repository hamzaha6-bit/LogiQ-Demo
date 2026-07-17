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


def get_sheets_service(user_id: str):
    return build("sheets", "v4", credentials=get_credentials(user_id), cache_discovery=False)


def get_calendar_service(user_id: str):
    return build("calendar", "v3", credentials=get_credentials(user_id), cache_discovery=False)


def _require_calendar(user_id: str, *, write: bool = False) -> None:
    if not load_user_token(user_id):
        raise PermissionError("Connect Google first — /api/auth/gmail/connect")
    if write:
        if not has_scope(user_id, CALENDAR_SCOPE):
            raise PermissionError("Re-authorise Google for Calendar write access")
    else:
        if not has_scope(user_id, CALENDAR_SCOPE) and not has_scope(user_id, "calendar.readonly"):
            raise PermissionError("Re-authorise Google for Calendar access")


def check_availability(
    user_id: str,
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
) -> Dict[str, Any]:
    _require_calendar(user_id, write=False)
    if not (time_min or "").strip() or not (time_max or "").strip():
        raise ValueError("time_min and time_max are required")
    cal = (calendar_id or "primary").strip()
    service = get_calendar_service(user_id)
    result = (
        service.freebusy()
        .query(body={"timeMin": time_min, "timeMax": time_max, "items": [{"id": cal}]})
        .execute()
    )
    busy = result.get("calendars", {}).get(cal, {}).get("busy", [])
    return {
        "success": True,
        "calendar_id": cal,
        "time_min": time_min,
        "time_max": time_max,
        "busy": busy,
        "busy_count": len(busy),
    }


def list_events(
    user_id: str,
    *,
    time_min: str = "",
    time_max: str = "",
    calendar_id: str = "primary",
    max_results: int = 25,
    query: str = "",
) -> Dict[str, Any]:
    _require_calendar(user_id, write=False)
    cal = (calendar_id or "primary").strip()
    try:
        limit = max(1, min(int(max_results or 25), 100))
    except (TypeError, ValueError):
        limit = 25
    kwargs: Dict[str, Any] = {
        "calendarId": cal,
        "maxResults": limit,
        "singleEvents": True,
        "orderBy": "startTime",
    }
    if (time_min or "").strip():
        kwargs["timeMin"] = time_min.strip()
    if (time_max or "").strip():
        kwargs["timeMax"] = time_max.strip()
    if (query or "").strip():
        kwargs["q"] = query.strip()
    service = get_calendar_service(user_id)
    result = service.events().list(**kwargs).execute()
    items = result.get("items") or []
    events = []
    for ev in items:
        events.append({
            "event_id": ev.get("id"),
            "summary": ev.get("summary", ""),
            "description": ev.get("description", ""),
            "start": (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date"),
            "end": (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date"),
            "html_link": ev.get("htmlLink"),
            "status": ev.get("status"),
            "attendees": [a.get("email") for a in (ev.get("attendees") or []) if a.get("email")],
        })
    return {"success": True, "calendar_id": cal, "events": events, "count": len(events)}


def create_event(
    user_id: str,
    *,
    title: str,
    start: str,
    end: str,
    description: str = "",
    attendees: Optional[List[str]] = None,
    calendar_id: str = "primary",
    timezone_name: str = "UTC",
    send_updates: str = "",
) -> Dict[str, Any]:
    _require_calendar(user_id, write=True)
    summary = (title or "").strip()
    if not summary or not (start or "").strip() or not (end or "").strip():
        raise ValueError("title, start, and end are required")
    attendee_list = [{"email": e.strip()} for e in (attendees or []) if e and str(e).strip()]
    cal = (calendar_id or "primary").strip()
    event_body: Dict[str, Any] = {
        "summary": summary,
        "description": description or "",
        "start": {"dateTime": start.strip(), "timeZone": timezone_name or "UTC"},
        "end": {"dateTime": end.strip(), "timeZone": timezone_name or "UTC"},
    }
    if attendee_list:
        event_body["attendees"] = attendee_list
    updates = send_updates or ("all" if attendee_list else "none")
    service = get_calendar_service(user_id)
    created = (
        service.events()
        .insert(calendarId=cal, body=event_body, sendUpdates=updates)
        .execute()
    )
    if not created.get("id"):
        raise RuntimeError("Calendar create returned no event id")
    return {
        "success": True,
        "event_id": created.get("id"),
        "html_link": created.get("htmlLink"),
        "summary": created.get("summary"),
        "start": start.strip(),
        "end": end.strip(),
        "attendees": [a["email"] for a in attendee_list],
        "send_updates": updates,
    }


def update_event(
    user_id: str,
    event_id: str,
    *,
    title: str = "",
    start: str = "",
    end: str = "",
    description: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    calendar_id: str = "primary",
    timezone_name: str = "UTC",
) -> Dict[str, Any]:
    _require_calendar(user_id, write=True)
    eid = (event_id or "").strip()
    if not eid:
        raise ValueError("event_id is required")
    cal = (calendar_id or "primary").strip()
    service = get_calendar_service(user_id)
    existing = service.events().get(calendarId=cal, eventId=eid).execute()
    if title:
        existing["summary"] = title.strip()
    if description is not None:
        existing["description"] = description
    if start:
        existing["start"] = {"dateTime": start.strip(), "timeZone": timezone_name or "UTC"}
    if end:
        existing["end"] = {"dateTime": end.strip(), "timeZone": timezone_name or "UTC"}
    if attendees is not None:
        existing["attendees"] = [{"email": e.strip()} for e in attendees if e and str(e).strip()]
    updated = service.events().update(calendarId=cal, eventId=eid, body=existing).execute()
    if not updated.get("id"):
        raise RuntimeError("Calendar update returned no event id")
    return {
        "success": True,
        "event_id": updated.get("id"),
        "summary": updated.get("summary"),
        "html_link": updated.get("htmlLink"),
        "updated": True,
    }


def cancel_event(
    user_id: str,
    event_id: str,
    *,
    calendar_id: str = "primary",
    send_updates: str = "all",
) -> Dict[str, Any]:
    _require_calendar(user_id, write=True)
    eid = (event_id or "").strip()
    if not eid:
        raise ValueError("event_id is required")
    cal = (calendar_id or "primary").strip()
    service = get_calendar_service(user_id)
    service.events().delete(
        calendarId=cal,
        eventId=eid,
        sendUpdates=send_updates or "all",
    ).execute()
    return {"success": True, "event_id": eid, "cancelled": True, "calendar_id": cal}


def send_calendar_invite(
    user_id: str,
    *,
    title: str,
    start: str,
    end: str,
    attendees: List[str],
    description: str = "",
    calendar_id: str = "primary",
    timezone_name: str = "UTC",
) -> Dict[str, Any]:
    """Create an event and email invites to attendees (sendUpdates=all)."""
    if not attendees:
        raise ValueError("attendees are required for a calendar invite")
    return create_event(
        user_id,
        title=title,
        start=start,
        end=end,
        description=description,
        attendees=attendees,
        calendar_id=calendar_id,
        timezone_name=timezone_name,
        send_updates="all",
    )
