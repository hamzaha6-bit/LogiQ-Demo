from http.server import BaseHTTPRequestHandler
import json
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from supabase import create_client

try:
    from hook_handler import handle_user_created_hook, is_user_created_hook_path
except ImportError:
    handle_user_created_hook = None

    def is_user_created_hook_path(_path: str) -> bool:
        return False


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if is_user_created_hook_path(path) and handle_user_created_hook:
            from hook_handler import json_response

            json_response(
                self,
                200,
                {"status": "ok", "hook": "user-created", "methods": ["GET", "POST"]},
            )
            return
        if path.endswith("/me"):
            self._me()
        else:
            self._json(404, {"detail": f"Unknown auth route: {path}"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

        if is_user_created_hook_path(path) and handle_user_created_hook:
            handle_user_created_hook(self)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"detail": f"Invalid JSON body: {exc}"})
            return

        url = (os.environ.get("SUPABASE_URL") or "").strip()
        anon_key = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
        if not url or not anon_key:
            self._json(503, {"detail": "Supabase not configured — set SUPABASE_URL and SUPABASE_ANON_KEY"})
            return

        if path.endswith("/signup"):
            self._signup(body, url, anon_key)
        elif path.endswith("/login"):
            self._login(body, url, anon_key)
        elif path.endswith("/logout"):
            self._json(200, {"success": True})
        else:
            self._json(404, {"detail": f"Unknown auth route: {path}"})

    def _me(self):
        url = (os.environ.get("SUPABASE_URL") or "").strip()
        anon_key = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
        if not url or not anon_key:
            self._json(503, {"detail": "Supabase not configured"})
            return

        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not token:
            self._json(401, {"detail": "Not authenticated"})
            return

        try:
            client = create_client(url, anon_key)
            result = client.auth.get_user(token)
            user = result.user
            if not user:
                self._json(401, {"detail": "Not authenticated"})
                return

            meta = user.user_metadata or {}
            name = meta.get("name") or (user.email or "").split("@")[0]
            self._json(
                200,
                {
                    "id": str(user.id),
                    "email": user.email or "",
                    "name": name,
                    "plan": "starter",
                    "onboarding_complete": False,
                },
            )
        except Exception as exc:
            self._json(401, {"detail": str(exc) or "Not authenticated"})

    def _signup(self, body, url, anon_key):
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not name or not email or not password:
            self._json(400, {"detail": "name, email, and password are required"})
            return

        try:
            client = create_client(url, anon_key)
            result = client.auth.sign_up(
                {
                    "email": email,
                    "password": password,
                    "options": {"data": {"name": name}},
                }
            )
        except Exception as exc:
            self._json(400, {"detail": str(exc)})
            return

        user = result.user
        session = result.session
        if not user:
            self._json(400, {"detail": "Signup failed — check email confirmation settings"})
            return

        user_id = str(user.id)
        if not session:
            self._json(
                200,
                {
                    "user": {"id": user_id, "email": email, "name": name},
                    "access_token": "",
                    "message": "Check your email to confirm your account",
                },
            )
            return

        self._json(
            200,
            {
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
                "expires_in": session.expires_in,
                "user": {"id": user_id, "email": email, "name": name},
            },
        )

    def _login(self, body, url, anon_key):
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or not password:
            self._json(400, {"detail": "email and password are required"})
            return

        try:
            client = create_client(url, anon_key)
            result = client.auth.sign_in_with_password({"email": email, "password": password})
        except Exception:
            self._json(401, {"detail": "Invalid email or password"})
            return

        if not result.session or not result.user:
            self._json(401, {"detail": "Invalid email or password"})
            return

        user_id = str(result.user.id)
        meta = result.user.user_metadata or {}
        name = meta.get("name") or email.split("@")[0]
        session = result.session
        self._json(
            200,
            {
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
                "expires_in": session.expires_in,
                "user": {"id": user_id, "email": email, "name": name},
            },
        )

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
