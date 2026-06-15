from http.server import BaseHTTPRequestHandler
import json
from urllib.parse import parse_qs, urlparse

from http_auth import resolve_user_id
from sheets_service import SchemaMismatchError, SheetsError, connect, connection_status, poll, read_sheet, write_row


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
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

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
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
                if not url:
                    self._json(400, {"detail": "Sheet URL is required"})
                    return
                row = body.get("row") or body.get("row_data") or {}
                self._json(200, write_row(url, agent, user_id, row))
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

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
