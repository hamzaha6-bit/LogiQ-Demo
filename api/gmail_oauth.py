"""Gmail / Google OAuth route handlers — imported by auth.py (not a Vercel entry)."""
from __future__ import annotations

import base64
import json
import os
import secrets
from typing import Optional, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from google_auth_oauthlib.flow import Flow

from google_oauth import GOOGLE_SCOPES, check_gmail_health, is_oauth_configured, parse_client_config, save_user_token
from http_auth import resolve_access_token, resolve_user_id
from supabase_rest import user_id_from_bearer

GMAIL_REDIRECT_URI = os.environ.get(
    "GMAIL_REDIRECT_URI", "https://app.logiqops.co.uk/api/auth/gmail/callback"
).strip()


def _frontend_url() -> str:
    return os.environ.get("FRONTEND_URL", "https://app.logiqops.co.uk").strip().rstrip("/")


def _gmail_redirect(status: str, access_token: Optional[str] = None, reason: Optional[str] = None) -> str:
    params = {"gmail": status}
    if access_token:
        params["token"] = access_token
    if reason:
        params["reason"] = reason
    return f"{_frontend_url()}?{urlencode(params)}"


def _redirect_uri_from_auth_url(auth_url: str) -> str:
    params = parse_qs(urlparse(auth_url).query)
    values = params.get("redirect_uri") or []
    return unquote(values[0]) if values else ""


def _log_redirect_uri(context: str, auth_url: str = "") -> None:
    configured = GMAIL_REDIRECT_URI
    from_env = (os.environ.get("GMAIL_REDIRECT_URI") or "").strip()
    in_auth_url = _redirect_uri_from_auth_url(auth_url) if auth_url else ""
    print(f"[gmail_auth] {context} GMAIL_REDIRECT_URI (code): {configured!r} len={len(configured)}")
    print(f"[gmail_auth] {context} GMAIL_REDIRECT_URI (env): {from_env!r} len={len(from_env)}")
    if in_auth_url:
        print(f"[gmail_auth] {context} redirect_uri in auth URL: {in_auth_url!r} len={len(in_auth_url)}")
        print(f"[gmail_auth] {context} auth URL matches configured: {in_auth_url == configured}")


def _redirect_uri_info() -> dict:
    from_env = (os.environ.get("GMAIL_REDIRECT_URI") or "").strip()
    return {
        "redirect_uri": GMAIL_REDIRECT_URI,
        "redirect_uri_length": len(GMAIL_REDIRECT_URI),
        "redirect_uri_from_env": from_env or None,
        "redirect_uri_source": "GMAIL_REDIRECT_URI env" if from_env else "code default",
    }


def _build_flow() -> Flow:
    flow = Flow.from_client_config(parse_client_config(), scopes=GOOGLE_SCOPES, redirect_uri=GMAIL_REDIRECT_URI)
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


def is_gmail_auth_path(path: str) -> bool:
    return "/auth/gmail" in (path or "")


def handle_connect(handler) -> None:
    if not is_oauth_configured():
        handler._json(503, {"detail": "Google OAuth not configured"})
        return
    access_token = resolve_access_token(handler)
    user_id = user_id_from_bearer(access_token) if access_token else None
    try:
        flow = _build_flow()
        state = _encode_oauth_state(user_id, access_token)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
            state=state,
        )
        _log_redirect_uri("connect", auth_url)
        handler.send_response(302)
        handler.send_header("Location", auth_url)
        handler.end_headers()
    except Exception as exc:
        handler._json(500, {"detail": f"Google OAuth error: {exc}"})


def handle_callback(handler) -> None:
    qs = parse_qs(urlparse(handler.path).query)
    error = (qs.get("error") or [""])[0]
    if error:
        _redirect(handler, _gmail_redirect("error", reason=error))
        return

    code = (qs.get("code") or [""])[0]
    state = (qs.get("state") or [""])[0]
    if not code:
        _redirect(handler, _gmail_redirect("error", reason="missing_code"))
        return
    if not is_oauth_configured():
        _redirect(handler, _gmail_redirect("error", reason="not_configured"))
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
            save_user_token(user_id, token_data)
        _redirect(handler, _gmail_redirect("connected", access_token=access_token))
    except Exception as exc:
        _log_redirect_uri("callback (token exchange failed)")
        print(f"[gmail_auth] callback exception: {exc!r}")
        reason = "redirect_uri_mismatch" if "redirect_uri" in str(exc).lower() else "oauth_error"
        _redirect(handler, _gmail_redirect("error", access_token=access_token, reason=reason))


def handle_status(handler) -> None:
    user_id = resolve_user_id(handler)
    base = {**_redirect_uri_info(), "configured": is_oauth_configured()}
    if not user_id:
        handler._json(200, {**base, "connected": False, "healthy": False})
        return
    health = check_gmail_health(user_id)
    handler._json(
        200,
        {
            **base,
            "connected": health.get("connected", False),
            "healthy": health.get("healthy", False),
            "email": health.get("email", ""),
            "sheets_scope": health.get("sheets_scope", False),
            "calendar_scope": health.get("calendar_scope", False),
            "error": health.get("error", ""),
        },
    )


def _redirect(handler, url: str) -> None:
    handler.send_response(302)
    handler.send_header("Location", url)
    handler.end_headers()
