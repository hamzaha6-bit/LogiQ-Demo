from http.server import BaseHTTPRequestHandler
import json
import os
import sys
from urllib.parse import urlparse

_API_DIR = os.path.dirname(os.path.abspath(__file__))
_API_LIB = os.path.normpath(os.path.join(_API_DIR, "..", "api_lib"))
if _API_LIB not in sys.path:
    sys.path.insert(0, _API_LIB)

from supabase import create_client

from supabase_rest import (
    postgrest_error_code,
    rest_get,
    rest_get_with_error,
    rest_patch_with_error,
    rest_post_with_error,
    sanitize_postgrest_error,
    user_id_from_bearer,
)

try:
    from hook_handler import handle_user_created_hook, json_response as hook_json_response
except ImportError:
    handle_user_created_hook = None
    hook_json_response = None

try:
    from gmail_oauth import handle_callback, handle_connect, handle_disconnect, handle_status, is_gmail_auth_path
except ImportError:
    is_gmail_auth_path = lambda _p: False
    handle_connect = handle_callback = handle_status = handle_disconnect = None


def _is_user_created_hook_path(path: str) -> bool:
    normalized = (path or "").rstrip("/").lower()
    return normalized.endswith("/hook/user-created")


def _profile_row(user_id: str):
    rows, status, body = rest_get_with_error(
        "user_profiles",
        {
            "id": f"eq.{user_id}",
            "select": "name,plan,onboarding_complete,tos_accepted_at,tos_version_accepted,onboarding_vertical,onboarding_pain_points,onboarding_completed_at,welcome_sent_at",
        },
    )
    if status >= 400:
        print(
            f"[auth] profile read failed user_id={user_id} "
            f"status={status} body={sanitize_postgrest_error(body)}"
        )
        return None
    return rows[0] if rows else None


def _postgrest_failure(step: str, status: int, body: str) -> dict:
    safe = sanitize_postgrest_error(body)
    code = postgrest_error_code(safe)
    print(f"[auth] profile {step} postgrest error status={status} code={code} body={safe}")
    return {
        "detail": f"Profile update failed during {step}",
        "postgrest_status": status,
        "postgrest_error": safe,
        "postgrest_code": code,
    }


def _ensure_profile(user_id: str, name: str = "", email: str = ""):
    existing = _profile_row(user_id)
    if existing:
        return existing
    row, err = rest_post_with_error(
        "user_profiles",
        {
            "id": user_id,
            "name": name or (email.split("@")[0] if email else "User"),
            "plan": "starter",
            "onboarding_complete": False,
        },
        on_conflict="id",
    )
    if row:
        return row
    print(f"[auth] ensure_profile failed user_id={user_id}: {err}")
    return {"name": name, "plan": "starter", "onboarding_complete": False}


def _profile_response(user_id: str, profile: dict) -> dict:
    return {
        "id": user_id,
        "name": profile.get("name") or "",
        "plan": profile.get("plan") or "starter",
        "onboarding_complete": bool(profile.get("onboarding_complete")),
        "tos_accepted_at": profile.get("tos_accepted_at"),
        "tos_version_accepted": profile.get("tos_version_accepted"),
        "onboarding_vertical": profile.get("onboarding_vertical"),
        "onboarding_pain_points": profile.get("onboarding_pain_points"),
        "onboarding_completed_at": profile.get("onboarding_completed_at"),
        "welcome_sent_at": profile.get("welcome_sent_at"),
    }


def _profile_payload(user_id: str, email: str, fallback_name: str):
    profile = _ensure_profile(user_id, fallback_name, email)
    payload = _profile_response(user_id, profile)
    payload["email"] = email
    if not payload["name"]:
        payload["name"] = fallback_name
    try:
        from admin_dashboard import email_is_owner

        payload["is_owner"] = email_is_owner(email)
    except ImportError:
        payload["is_owner"] = False
    return payload


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if _is_user_created_hook_path(path):
            if hook_json_response and handle_user_created_hook:
                hook_json_response(
                    self,
                    200,
                    {"status": "ok", "hook": "user-created", "methods": ["GET", "POST"]},
                )
            else:
                self._json(
                    200,
                    {
                        "status": "ok",
                        "hook": "user-created",
                        "methods": ["GET", "POST"],
                        "via": "auth.py-fallback",
                    },
                )
            return
        if is_gmail_auth_path(path):
            if path.endswith("/connect") and handle_connect:
                handle_connect(self)
            elif path.endswith("/callback") and handle_callback:
                handle_callback(self)
            elif path.endswith("/status") and handle_status:
                handle_status(self)
            else:
                self._json(404, {"detail": f"Unknown Gmail auth route: {path}"})
            return
        if path.endswith("/me"):
            self._me()
        else:
            self._json(404, {"detail": f"Unknown auth route: {path}"})

    def do_PATCH(self):
        path = urlparse(self.path).path.rstrip("/")
        if path.endswith("/profile"):
            self._profile_patch()
        else:
            self._json(404, {"detail": f"Unknown auth route: {path}"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

        if _is_user_created_hook_path(path):
            if handle_user_created_hook:
                handle_user_created_hook(self)
            else:
                self._json(503, {"detail": "Hook handler unavailable"})
            return

        if is_gmail_auth_path(path) and path.endswith("/disconnect") and handle_disconnect:
            handle_disconnect(self)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"detail": f"Invalid JSON body: {exc}"})
            return

        # Post-OTP welcome email (frontend calls after successful verifyOtp).
        # FLAG: This is the post-confirm welcome path — prefer this over the Before User Created hook.
        if path.endswith("/welcome"):
            self._welcome_after_verify(body)
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
            user_id = str(user.id)
            self._json(200, _profile_payload(user_id, user.email or "", name))
        except Exception as exc:
            self._json(401, {"detail": str(exc) or "Not authenticated"})

    def _profile_patch(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not token:
            self._json(401, {"detail": "Not authenticated"})
            return

        user_id = user_id_from_bearer(token)
        if not user_id:
            self._json(401, {"detail": "Not authenticated"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            self._json(400, {"detail": f"Invalid JSON body: {exc}"})
            return

        updates = {}
        if "onboarding_complete" in body:
            updates["onboarding_complete"] = bool(body.get("onboarding_complete"))
        if "name" in body and body.get("name"):
            updates["name"] = str(body.get("name")).strip()
        if "plan" in body and body.get("plan"):
            updates["plan"] = str(body.get("plan")).strip()
        if "tos_accepted_at" in body and body.get("tos_accepted_at"):
            updates["tos_accepted_at"] = str(body.get("tos_accepted_at")).strip()
        if "tos_version_accepted" in body and body.get("tos_version_accepted"):
            updates["tos_version_accepted"] = str(body.get("tos_version_accepted")).strip()
        if "onboarding_vertical" in body:
            updates["onboarding_vertical"] = str(body.get("onboarding_vertical") or "").strip() or None
        if "onboarding_pain_points" in body:
            updates["onboarding_pain_points"] = str(body.get("onboarding_pain_points") or "").strip() or None
        if "onboarding_completed_at" in body and body.get("onboarding_completed_at"):
            updates["onboarding_completed_at"] = str(body.get("onboarding_completed_at")).strip()

        if not updates:
            self._json(400, {"detail": "No valid profile fields to update"})
            return

        _ensure_profile(user_id)
        ok, pg_status, pg_body = rest_patch_with_error("user_profiles", {"id": user_id}, updates)
        if not ok:
            payload = _postgrest_failure("patch", pg_status, pg_body)
            api_status = 502 if pg_status == 0 else min(max(pg_status, 400), 599)
            self._json(api_status, payload)
            return

        profile = _profile_row(user_id) or {}
        response = _profile_response(user_id, profile)
        # If PostgREST read lags (schema cache) but PATCH succeeded, return what we wrote.
        for key, value in updates.items():
            response[key] = value
        self._json(200, response)

    def _welcome_after_verify(self, body):
        """
        POST /api/auth/welcome — sole welcome-email path (after OTP / magic-link confirm).
        Idempotent via user_profiles.welcome_sent_at — skips if already sent.
        """
        try:
            from hook_handler import send_welcome_email
        except ImportError:
            self._json(503, {"detail": "Welcome mailer unavailable"})
            return

        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        email = ""
        name = ""
        user_id = ""
        if token:
            url = (os.environ.get("SUPABASE_URL") or "").strip()
            anon_key = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
            if url and anon_key:
                try:
                    client = create_client(url, anon_key)
                    result = client.auth.get_user(token)
                    user = result.user
                    if user:
                        user_id = str(user.id)
                        email = (user.email or "").strip()
                        meta = user.user_metadata or {}
                        name = (meta.get("name") or "").strip()
                except Exception as exc:
                    print(f"[auth] welcome get_user failed: {exc}")
        if not user_id and token:
            user_id = user_id_from_bearer(token) or ""
        if not email:
            email = (body.get("email") or "").strip()
        if not name:
            name = (body.get("name") or "").strip()
        if not email:
            self._json(400, {"detail": "email is required"})
            return
        if not user_id:
            self._json(401, {"detail": "Authentication required to send welcome email"})
            return

        profile = _ensure_profile(user_id, name, email)
        if profile.get("welcome_sent_at"):
            print(f"[auth] welcome already sent for user {user_id} — skipping")
            self._json(200, {"sent": False, "skipped": True, "detail": "already_sent"})
            return

        ok, detail = send_welcome_email(email, name)
        if ok:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            rest_patch_with_error("user_profiles", {"id": user_id}, {"welcome_sent_at": now})
        self._json(200, {"sent": ok, "skipped": False, "detail": detail})

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
        profile = _ensure_profile(user_id, name, email)
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
                "user": {
                    "id": user_id,
                    "email": email,
                    "name": profile.get("name") or name,
                    "plan": profile.get("plan") or "starter",
                    "onboarding_complete": bool(profile.get("onboarding_complete")),
                    "tos_accepted_at": profile.get("tos_accepted_at"),
                    "tos_version_accepted": profile.get("tos_version_accepted"),
                },
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
        profile = _ensure_profile(user_id, name, email)
        session = result.session
        self._json(
            200,
            {
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
                "expires_in": session.expires_in,
                "user": {
                    "id": user_id,
                    "email": email,
                    "name": profile.get("name") or name,
                    "plan": profile.get("plan") or "starter",
                    "onboarding_complete": bool(profile.get("onboarding_complete")),
                    "tos_accepted_at": profile.get("tos_accepted_at"),
                    "tos_version_accepted": profile.get("tos_version_accepted"),
                },
            },
        )

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
