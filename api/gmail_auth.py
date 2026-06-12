from http.server import BaseHTTPRequestHandler
import base64
import json
import os
import secrets
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from google_auth_oauthlib.flow import Flow
from supabase import create_client

GMAIL_REDIRECT_URI = "https://logiqops.co.uk/api/auth/gmail/callback"
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]


def _env(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def _frontend_url() -> str:
    return (_env("FRONTEND_URL") or _env("OAUTH_REDIRECT_BASE") or "https://logiqops.co.uk").rstrip("/")


def _parse_credentials_json() -> dict:
    raw = _env("GMAIL_CREDENTIALS_JSON")
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


def is_gmail_configured() -> bool:
    if not _env("GMAIL_SENDER_EMAIL") or not _env("GMAIL_CREDENTIALS_JSON"):
        return False
    try:
        _parse_credentials_json()
        return True
    except Exception:
        return False


def _build_flow() -> Flow:
    flow = Flow.from_client_config(
        _parse_credentials_json(),
        scopes=GMAIL_SCOPES,
        redirect_uri=GMAIL_REDIRECT_URI,
    )
    flow.oauth2session.redirect_uri = GMAIL_REDIRECT_URI
    return flow


def _encode_oauth_state(user_id: Optional[str]) -> str:
    payload = {"user_id": user_id or "", "nonce": secrets.token_urlsafe(16)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _decode_oauth_state(state: str) -> Optional[str]:
    if not state:
        return None
    try:
        pad = "=" * (-len(state) % 4)
        data = json.loads(base64.urlsafe_b64decode(state + pad))
        uid = (data.get("user_id") or "").strip()
        return uid or None
    except Exception:
        return None


def _user_id_from_access_token(token: str) -> Optional[str]:
    url, anon = _env("SUPABASE_URL"), _env("SUPABASE_ANON_KEY")
    if not token or not url or not anon:
        return None
    try:
        client = create_client(url, anon)
        user = client.auth.get_user(token).user
        return str(user.id) if user else None
    except Exception:
        return None


def _resolve_user_id(handler: BaseHTTPRequestHandler) -> Optional[str]:
    qs = parse_qs(urlparse(handler.path).query)
    token = (qs.get("token") or [""])[0]
    if token:
        uid = _user_id_from_access_token(token)
        if uid:
            return uid
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _user_id_from_access_token(auth[7:].strip())
    return None


def _supabase_rest_headers() -> dict:
    key = _env("SUPABASE_SERVICE_KEY")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def _save_user_token(user_id: str, token_data: dict) -> bool:
    url = _env("SUPABASE_URL").rstrip("/")
    service_key = _env("SUPABASE_SERVICE_KEY")
    if not user_id or not url or not service_key:
        return False
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            f"{url}/rest/v1/user_integrations",
            headers=_supabase_rest_headers(),
            params={"on_conflict": "user_id,integration"},
            json={"user_id": user_id, "integration": "gmail", "token_data": token_data},
        )
        return resp.status_code < 400


def _load_user_token(user_id: str) -> Optional[dict]:
    url = _env("SUPABASE_URL").rstrip("/")
    service_key = _env("SUPABASE_SERVICE_KEY")
    if not user_id or not url or not service_key:
        return None
    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"{url}/rest/v1/user_integrations",
            headers=_supabase_rest_headers(),
            params={
                "user_id": f"eq.{user_id}",
                "integration": "eq.gmail",
                "select": "token_data",
                "limit": "1",
            },
        )
        if resp.status_code == 200:
            rows = resp.json()
            if rows and rows[0].get("token_data"):
                return rows[0]["token_data"]
    return None


def _is_connected(user_id: Optional[str]) -> bool:
    if not user_id:
        return False
    data = _load_user_token(user_id)
    return bool(data and data.get("token"))


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        if path.endswith("/connect"):
            self._connect()
        elif path.endswith("/callback"):
            self._callback()
        elif path.endswith("/status"):
            self._status()
        else:
            self._json(404, {"detail": f"Unknown Gmail auth route: {path}"})

    def _connect(self):
        if not is_gmail_configured():
            self._json(503, {"detail": "Gmail not configured"})
            return
        user_id = _resolve_user_id(self)
        try:
            flow = _build_flow()
            state = _encode_oauth_state(user_id)
            auth_url, _ = flow.authorization_url(
                access_type="offline",
                prompt="consent",
                include_granted_scopes="true",
                state=state,
            )
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()
        except Exception as exc:
            self._json(500, {"detail": f"Gmail OAuth error: {exc}"})

    def _callback(self):
        frontend = _frontend_url()
        qs = parse_qs(urlparse(self.path).query)
        error = (qs.get("error") or [""])[0]
        if error:
            self._redirect(f"{frontend}/?gmail=error&reason={error}")
            return

        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        if not code:
            self._redirect(f"{frontend}/?gmail=error&reason=missing_code")
            return
        if not is_gmail_configured():
            self._redirect(f"{frontend}/?gmail=error&reason=not_configured")
            return

        user_id = _decode_oauth_state(state)
        try:
            flow = _build_flow()
            flow.fetch_token(code=code)
            creds = flow.credentials
            if not creds or not creds.token:
                raise RuntimeError("Empty credentials after token exchange")
            token_data = json.loads(creds.to_json())
            if user_id:
                _save_user_token(user_id, token_data)
            self._redirect(f"{frontend}/?gmail=connected")
        except Exception as exc:
            reason = "redirect_uri_mismatch" if "redirect_uri" in str(exc).lower() else "oauth_error"
            self._redirect(f"{frontend}/?gmail=error&reason={reason}")

    def _status(self):
        user_id = _resolve_user_id(self)
        self._json(
            200,
            {
                "connected": _is_connected(user_id),
                "configured": is_gmail_configured(),
            },
        )

    def _redirect(self, url: str):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
