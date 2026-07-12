"""All integration routes: config, sheets, calendar, gmail send."""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

_API_DIR = os.path.dirname(os.path.abspath(__file__))
_API_LIB = os.path.normpath(os.path.join(_API_DIR, "..", "api_lib"))
if _API_LIB not in sys.path:
    sys.path.insert(0, _API_LIB)

from execution_gate import check_execution_gate, record_allowed_action
from google_oauth import (
    CALENDAR_SCOPE,
    check_gmail_health,
    get_calendar_service,
    has_scope,
    is_oauth_configured,
    send_user_email,
)
from http_auth import resolve_user_id
from sheets_service import SchemaMismatchError, SheetsError, connect, connection_status, poll, read_sheet, write_row
from usage import record_email_sent


def _parse_iso(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("datetime is required")
    if value.endswith("Z"):
        return value
    if "+" in value[10:] or value.count("-") > 2:
        return value
    return value + "Z"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")

        if path.endswith("/integrations/config"):
            self._integrations_config()
        elif "/integrations/sheets/" in path:
            self._sheets_get(path)
        elif "/integrations/calendar/" in path:
            self._calendar_get(path)
        else:
            self._json(404, {"detail": f"Unknown route: {path}"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

        if path.endswith("/send/gmail"):
            self._send_gmail()
        elif "/integrations/sheets/" in path:
            self._sheets_post(path)
        elif path.endswith("/integrations/calendar/events"):
            self._calendar_create()
        else:
            self._json(404, {"detail": f"Unknown route: {path}"})

    def _integrations_config(self):
        user_id = resolve_user_id(self)
        oauth_ok = is_oauth_configured()
        sheets_scope = False
        calendar_scope = False
        gmail_authorised = False
        email = ""

        if user_id and oauth_ok:
            health = check_gmail_health(user_id)
            gmail_authorised = health.get("healthy", False)
            sheets_scope = health.get("sheets_scope", False)
            calendar_scope = health.get("calendar_scope", False)
            email = health.get("email", "")

        self._json(
            200,
            {
                "gmail": oauth_ok and gmail_authorised,
                "gmail_configured": oauth_ok,
                "gmail_authorised": gmail_authorised,
                "google_authorised": gmail_authorised,
                "sheets_configured": oauth_ok,
                "sheets_available": oauth_ok and sheets_scope,
                "sheets_scope": sheets_scope,
                "calendar_scope": calendar_scope,
                "calendar_configured": oauth_ok,
                "sender_email": email,
                "xero_configured": bool((os.environ.get("XERO_CLIENT_ID") or "").strip()),
                "hubspot_configured": bool((os.environ.get("HUBSPOT_API_KEY") or "").strip()),
                "calendly_link": (os.environ.get("CALENDLY_LINK") or "").strip(),
            },
        )

    def _sheets_get(self, path: str):
        qs = parse_qs(urlparse(self.path).query)
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Sign in required"})
            return

        url = (qs.get("url") or [""])[0].strip()
        agent = (qs.get("agent") or [""])[0].strip()

        try:
            if path.endswith("/read"):
                if not url:
                    self._json(400, {"detail": "url query parameter is required"})
                    return
                self._json(200, read_sheet(url, agent or "aria", user_id))
            elif path.endswith("/poll"):
                if not url or not agent:
                    self._json(400, {"detail": "url and agent query parameters are required"})
                    return
                self._json(200, poll(url, agent, user_id))
            elif path.endswith("/status"):
                if not url or not agent:
                    self._json(400, {"detail": "url and agent query parameters are required"})
                    return
                self._json(200, connection_status(user_id, agent, url))
            else:
                self._json(404, {"detail": f"Unknown sheets route: {path}"})
        except SchemaMismatchError as exc:
            self._json(409, {"detail": str(exc), "paused": True, "schema_mismatch": exc.diff})
        except SheetsError as exc:
            msg = str(exc)
            code = 401 if "Connect Google" in msg or "Re-authorise" in msg else 400
            self._json(code, {"detail": msg})
        except PermissionError as exc:
            self._json(401, {"detail": str(exc)})
        except Exception as exc:
            self._json(502, {"detail": str(exc)})

    def _sheets_post(self, path: str):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Sign in required"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"detail": f"Invalid JSON: {exc}"})
            return

        url = (body.get("url") or "").strip()
        agent = (body.get("agent") or body.get("agent_id") or "aria").strip()

        try:
            if path.endswith("/connect"):
                if not url:
                    self._json(400, {"detail": "Sheet URL is required"})
                    return
                self._json(200, connect(url, agent, user_id))
            elif path.endswith("/write"):
                gate = check_execution_gate(user_id, "integration")
                if not gate.allowed:
                    self._json(403, gate.as_error_payload())
                    return
                if not url:
                    self._json(400, {"detail": "Sheet URL is required"})
                    return
                row = body.get("row") or body.get("row_data") or {}
                result = write_row(url, agent, user_id, row)
                record_allowed_action(gate.client_id, "integration")
                self._json(200, result)
            else:
                self._json(404, {"detail": f"Unknown sheets route: {path}"})
        except SchemaMismatchError as exc:
            self._json(409, {"detail": str(exc), "paused": True, "schema_mismatch": exc.diff})
        except SheetsError as exc:
            msg = str(exc)
            code = 401 if "Connect Google" in msg or "Re-authorise" in msg else 400
            self._json(code, {"detail": msg})
        except PermissionError as exc:
            self._json(401, {"detail": str(exc)})
        except Exception as exc:
            self._json(502, {"detail": str(exc)})

    def _send_gmail(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Sign in required"})
            return

        gate = check_execution_gate(user_id, "integration")
        if not gate.allowed:
            self._json(403, gate.as_error_payload())
            return

        if not is_oauth_configured():
            self._json(503, {"detail": "Gmail not configured"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"detail": f"Invalid JSON: {exc}"})
            return

        to = (body.get("to") or "").strip()
        subject = (body.get("subject") or "").strip()
        msg_body = (body.get("body") or "").strip()
        from_name = (body.get("from_name") or "").strip()

        if not to or not subject:
            self._json(400, {"detail": "to and subject are required"})
            return

        health = check_gmail_health(user_id)
        if not health.get("healthy"):
            self._json(
                401,
                {
                    "detail": health.get("error") or "Gmail not connected — visit /api/auth/gmail/connect",
                    "health": health,
                },
            )
            return

        try:
            ok, message_id = send_user_email(user_id, to, subject, msg_body, from_name)
            if not ok:
                self._json(502, {"detail": message_id})
                return
            record_allowed_action(gate.client_id, "integration")
            record_email_sent(user_id)
            self._json(200, {"success": True, "message_id": message_id, "configured": True, "from": health.get("email")})
        except PermissionError as exc:
            self._json(401, {"detail": str(exc)})
        except Exception as exc:
            self._json(502, {"detail": str(exc)})

    def _calendar_get(self, path: str):
        qs = parse_qs(urlparse(self.path).query)
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Sign in required"})
            return
        if not is_oauth_configured():
            self._json(503, {"detail": "Google OAuth not configured"})
            return
        if not has_scope(user_id, CALENDAR_SCOPE) and not has_scope(user_id, "calendar.readonly"):
            self._json(401, {"detail": "Re-authorise Google for Calendar access"})
            return

        try:
            if path.endswith("/availability"):
                time_min = _parse_iso((qs.get("time_min") or [""])[0])
                time_max = _parse_iso((qs.get("time_max") or [""])[0])
                calendar_id = (qs.get("calendar_id") or ["primary"])[0]
                self._json(200, _freebusy(user_id, calendar_id, time_min, time_max))
            elif path.endswith("/status"):
                health = check_gmail_health(user_id)
                self._json(
                    200,
                    {
                        "calendar_scope": health.get("calendar_scope", False),
                        "healthy": health.get("healthy", False),
                        "email": health.get("email", ""),
                    },
                )
            else:
                self._json(404, {"detail": f"Unknown calendar route: {path}"})
        except PermissionError as exc:
            self._json(401, {"detail": str(exc)})
        except ValueError as exc:
            self._json(400, {"detail": str(exc)})
        except Exception as exc:
            self._json(502, {"detail": str(exc)})

    def _calendar_create(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Sign in required"})
            return

        gate = check_execution_gate(user_id, "integration")
        if not gate.allowed:
            self._json(403, gate.as_error_payload())
            return

        if not is_oauth_configured():
            self._json(503, {"detail": "Google OAuth not configured"})
            return
        if not has_scope(user_id, CALENDAR_SCOPE):
            self._json(401, {"detail": "Re-authorise Google for Calendar write access"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"detail": f"Invalid JSON: {exc}"})
            return

        try:
            result = _create_event(user_id, body)
            record_allowed_action(gate.client_id, "integration")
            self._json(201, result)
        except PermissionError as exc:
            self._json(401, {"detail": str(exc)})
        except ValueError as exc:
            self._json(400, {"detail": str(exc)})
        except Exception as exc:
            self._json(502, {"detail": str(exc)})

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


def _freebusy(user_id: str, calendar_id: str, time_min: str, time_max: str) -> dict:
    service = get_calendar_service(user_id)
    result = (
        service.freebusy()
        .query(body={"timeMin": time_min, "timeMax": time_max, "items": [{"id": calendar_id}]})
        .execute()
    )
    busy = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    return {"success": True, "calendar_id": calendar_id, "busy": busy}


def _create_event(user_id: str, body: dict) -> dict:
    summary = (body.get("summary") or body.get("title") or "").strip()
    start = body.get("start") or body.get("start_time") or ""
    end = body.get("end") or body.get("end_time") or ""
    if not summary or not start or not end:
        raise ValueError("summary, start, and end are required")

    attendees = [{"email": e.strip()} for e in (body.get("attendees") or []) if e and str(e).strip()]
    calendar_id = (body.get("calendar_id") or "primary").strip()
    timezone_name = (body.get("timezone") or "UTC").strip()

    event_body = {
        "summary": summary,
        "description": body.get("description") or "",
        "start": {"dateTime": _parse_iso(str(start)), "timeZone": timezone_name},
        "end": {"dateTime": _parse_iso(str(end)), "timeZone": timezone_name},
    }
    if attendees:
        event_body["attendees"] = attendees

    send_updates = body.get("send_updates") or ("all" if attendees else "none")
    service = get_calendar_service(user_id)
    created = (
        service.events()
        .insert(calendarId=calendar_id, body=event_body, sendUpdates=send_updates)
        .execute()
    )
    return {
        "success": True,
        "event_id": created.get("id"),
        "html_link": created.get("htmlLink"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
