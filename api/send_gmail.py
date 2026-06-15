from http.server import BaseHTTPRequestHandler
import json

from google_oauth import check_gmail_health, is_oauth_configured, send_user_email
from http_auth import resolve_user_id


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Sign in required"})
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
            self._json(200, {"success": True, "message_id": message_id, "configured": True, "from": health.get("email")})
        except PermissionError as exc:
            self._json(401, {"detail": str(exc)})
        except Exception as exc:
            self._json(502, {"detail": str(exc)})

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
