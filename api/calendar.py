from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from google_oauth import CALENDAR_SCOPE, check_gmail_health, get_calendar_service, has_scope, is_oauth_configured
from http_auth import resolve_user_id


def _parse_iso(value: str) -> str:
    """Normalise to RFC3339 for Google Calendar API."""
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

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Sign in required"})
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
            if path.endswith("/events"):
                self._json(201, _create_event(user_id, body))
            else:
                self._json(404, {"detail": f"Unknown calendar route: {path}"})
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
        .query(
            body={
                "timeMin": time_min,
                "timeMax": time_max,
                "items": [{"id": calendar_id}],
            }
        )
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
