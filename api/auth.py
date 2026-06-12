from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import urlparse

from supabase import create_client


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

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
        else:
            self._json(404, {"detail": f"Unknown auth route: {path}"})

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
