from http.server import BaseHTTPRequestHandler
import base64
import json
import os
import secrets
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

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
    return os.environ.get("FRONTEND_URL", "https://logiqops.co.uk").strip().rstrip("/")


def _gmail_redirect(status: str, access_token: Optional[str] = None, reason: Optional[str] = None) -> str:
    """Build post-OAuth redirect without double slashes: base?gmail=...&token=..."""
    params = {"gmail": status}
    if access_token:
        params["token"] = access_token
    if reason:
        params["reason"] = reason
    return f"{_frontend_url()}?{urlencode(params)}"


def _log_redirect(context: str, url: str) -> None:
    frontend_env = os.environ.get("FRONTEND_URL")
    print(f"[gmail_auth] {context} FRONTEND_URL from os.environ: {frontend_env!r}")
    print(f"[gmail_auth] {context} resolved frontend base: {_frontend_url()!r}")
    print(f"[gmail_auth] {context} full redirect URL: {url}")


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


def _encode_oauth_state(user_id: Optional[str], access_token: Optional[str] = None) -> str:
    payload = {
        "user_id": user_id or "",
        "access_token": access_token or "",
        "nonce": secrets.token_urlsafe(16),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _decode_oauth_state(state: str) -> Tuple[Optional[str], Optional[str]]:
    if not state:
        return None, None
    try:
        pad = "=" * (-len(state) % 4)
        data = json.loads(base64.urlsafe_b64decode(state + pad))
        uid = (data.get("user_id") or "").strip()
        token = (data.get("access_token") or "").strip()
        return uid or None, token or None
    except Exception:
        return None, None


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


def _resolve_access_token(handler: BaseHTTPRequestHandler) -> Optional[str]:
    qs = parse_qs(urlparse(handler.path).query)
    token = (qs.get("token") or [""])[0]
    if token:
        return token
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def _resolve_user_id(handler: BaseHTTPRequestHandler) -> Optional[str]:
    token = _resolve_access_token(handler)
    if token:
        return _user_id_from_access_token(token)
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
        access_token = _resolve_access_token(self)
        user_id = _user_id_from_access_token(access_token) if access_token else None
        try:
            flow = _build_flow()
            state = _encode_oauth_state(user_id, access_token)
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
        qs = parse_qs(urlparse(self.path).query)
        error = (qs.get("error") or [""])[0]
        if error:
            url = _gmail_redirect("error", reason=error)
            _log_redirect("callback (error)", url)
            self._redirect(url)
            return

        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        if not code:
            url = _gmail_redirect("error", reason="missing_code")
            _log_redirect("callback (missing code)", url)
            self._redirect(url)
            return
        if not is_gmail_configured():
            url = _gmail_redirect("error", reason="not_configured")
            _log_redirect("callback (not configured)", url)
            self._redirect(url)
            return

        user_id, access_token = _decode_oauth_state(state)
        try:
            flow = _build_flow()
            flow.fetch_token(code=code)
            creds = flow.credentials
            if not creds or not creds.token:
                raise RuntimeError("Empty credentials after token exchange")
            token_data = json.loads(creds.to_json())
            if user_id:
                _save_user_token(user_id, token_data)
            url = _gmail_redirect("connected", access_token=access_token)
            _log_redirect("callback (success)", url)
            self._redirect(url)
        except Exception as exc:
            reason = "redirect_uri_mismatch" if "redirect_uri" in str(exc).lower() else "oauth_error"
            url = _gmail_redirect("error", access_token=access_token, reason=reason)
            _log_redirect("callback (exception)", url)
            self._redirect(url)

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
