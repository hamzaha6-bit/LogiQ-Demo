"""Gmail / Google OAuth route handlers — imported by auth.py (not a Vercel entry)."""
from __future__ import annotations

import base64
import json
import os
import secrets
import traceback
from typing import Optional, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urlparse

# Allow Google to return a subset/superset of requested scopes without failing exchange.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google_auth_oauthlib.flow import Flow

from google_oauth import GOOGLE_SCOPES, check_gmail_health, disconnect_user_token, is_oauth_configured, parse_client_config, save_user_token
from http_auth import resolve_access_token, resolve_user_id
from supabase_rest import user_id_from_bearer

GMAIL_REDIRECT_URI = os.environ.get(
    "GMAIL_REDIRECT_URI", "https://app.logiqops.co.uk/api/auth/gmail/callback"
).strip()


def _frontend_url() -> str:
    return os.environ.get("FRONTEND_URL", "https://app.logiqops.co.uk").strip().rstrip("/")


def _gmail_redirect(
    status: str,
    access_token: Optional[str] = None,
    reason: Optional[str] = None,
    *,
    error_detail: str = "",
    stage: str = "",
) -> str:
    params = {"gmail": status}
    if access_token:
        params["token"] = access_token
    if reason:
        params["reason"] = reason
    if error_detail:
        params["error_detail"] = error_detail[:180]
    if stage:
        params["stage"] = stage
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
    # Keep state small — only user_id + nonce. Do NOT embed JWT (truncates in Google redirect).
    payload = {
        "user_id": user_id or "",
        "nonce": secrets.token_urlsafe(16),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _decode_oauth_state(state: str) -> Tuple[Optional[str], Optional[str]]:
    if not state:
        print("[gmail_auth] state decode: empty state param")
        return None, None
    try:
        pad = "=" * (-len(state) % 4)
        data = json.loads(base64.urlsafe_b64decode(state + pad))
        uid = (data.get("user_id") or "").strip()
        # Legacy payloads may include access_token — ignore for save, session restored via localStorage
        token = (data.get("access_token") or "").strip()
        return uid or None, token or None
    except Exception as exc:
        print(f"[gmail_auth] state decode FAILED: {type(exc).__name__}: {exc} state_len={len(state)}")
        return None, None


def is_gmail_auth_path(path: str) -> bool:
    return "/auth/gmail" in (path or "")


def handle_connect(handler) -> None:
    if not is_oauth_configured():
        handler._json(503, {"detail": "Google OAuth not configured"})
        return
    access_token = resolve_access_token(handler)
    user_id = user_id_from_bearer(access_token) if access_token else None
    print(f"[gmail_auth] connect — has_token={bool(access_token)} user_id={user_id or '(missing)'}")
    if not user_id:
        handler._json(
            401,
            {"detail": "Sign in required before connecting Gmail — session token missing or invalid"},
        )
        return
    try:
        flow = _build_flow()
        state = _encode_oauth_state(user_id)
        print(f"[gmail_auth] connect — state_len={len(state)} user_id={user_id}")
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


def _callback_authorization_response(handler) -> str:
    """Rebuild the full callback URL Google redirected to (for token exchange)."""
    host = (handler.headers.get("Host") or "app.logiqops.co.uk").strip()
    proto = (handler.headers.get("X-Forwarded-Proto") or "https").split(",")[0].strip()
    path = handler.path if handler.path.startswith("/") else f"/{handler.path}"
    return f"{proto}://{host}{path}"


def _oauth_error_reason(exc: Exception) -> str:
    msg = str(exc).lower()
    if "redirect_uri" in msg:
        return "redirect_uri_mismatch"
    if "invalid_client" in msg or "client_secret" in msg:
        return "invalid_client_secret"
    if "scope" in msg and "changed" in msg:
        return "scope_mismatch"
    if "invalid_grant" in msg:
        return "invalid_grant"
    return "oauth_error"


def _log_callback_start(handler, qs: dict) -> None:
    code = (qs.get("code") or [""])[0]
    error = (qs.get("error") or [""])[0]
    state = (qs.get("state") or [""])[0]
    error_description = (qs.get("error_description") or [""])[0]
    auth_response = _callback_authorization_response(handler)

    print("[gmail_auth] === OAuth callback received ===")
    print(f"[gmail_auth] authorization_response: {auth_response!r}")
    print(f"[gmail_auth] code: {'(missing)' if not code else f'{code[:20]}… ({len(code)} chars)'}")
    print(f"[gmail_auth] error: {error or '(none)'}")
    print(f"[gmail_auth] error_description: {error_description or '(none)'}")
    print(f"[gmail_auth] state present: {bool(state)} len={len(state)}")
    _log_redirect_uri("callback")


def _log_callback_failure(stage: str, exc: Exception) -> None:
    print(f"[gmail_auth] callback FAILED at stage={stage!r}: {type(exc).__name__}: {exc}")
    print(f"[gmail_auth] traceback:\n{traceback.format_exc()}")


def handle_callback(handler) -> None:
    qs = parse_qs(urlparse(handler.path).query)
    _log_callback_start(handler, qs)

    error = (qs.get("error") or [""])[0]
    error_description = (qs.get("error_description") or [""])[0]
    if error:
        detail = error_description or error
        print(f"[gmail_auth] Google returned error: {error} — {error_description}")
        _redirect(
            handler,
            _gmail_redirect("error", reason=error, error_detail=detail, stage="google_error"),
        )
        return

    code = (qs.get("code") or [""])[0]
    state = (qs.get("state") or [""])[0]
    if not code:
        _redirect(handler, _gmail_redirect("error", reason="missing_code", stage="missing_code"))
        return
    if not is_oauth_configured():
        _redirect(handler, _gmail_redirect("error", reason="not_configured", stage="not_configured"))
        return

    user_id, access_token = _decode_oauth_state(state)
    print(f"[gmail_auth] decoded state user_id: {user_id or '(missing)'}")
    print(f"[gmail_auth] decoded state has access_token: {bool(access_token)}")

    if not user_id:
        print("[gmail_auth] ERROR: no user_id in OAuth state — cannot save token")
        _redirect(
            handler,
            _gmail_redirect(
                "error",
                access_token=access_token,
                reason="missing_user_id",
                error_detail="OAuth state did not contain user_id — connect while signed in",
                stage="missing_user_id",
            ),
        )
        return

    auth_response = _callback_authorization_response(handler)
    try:
        flow = _build_flow()
        print(f"[gmail_auth] token exchange redirect_uri: {flow.oauth2session.redirect_uri!r}")
        flow.fetch_token(authorization_response=auth_response)
        creds = flow.credentials
        if not creds or not creds.token:
            raise RuntimeError("Empty credentials after token exchange")
        token_data = json.loads(creds.to_json())
        print(
            f"[gmail_auth] token exchange OK — has_refresh_token={bool(token_data.get('refresh_token'))} "
            f"scopes={token_data.get('scopes') or token_data.get('scope')}"
        )
    except Exception as exc:
        _log_callback_failure("token_exchange", exc)
        _log_redirect_uri("callback (token exchange failed)")
        _redirect(
            handler,
            _gmail_redirect(
                "error",
                access_token=access_token,
                reason=_oauth_error_reason(exc),
                error_detail=str(exc),
                stage="token_exchange",
            ),
        )
        return

    if user_id:
        saved, save_error = save_user_token(user_id, token_data)
        print(f"[gmail_auth] Supabase save user_integrations: user_id={user_id} saved={saved} error={save_error or '(none)'}")
        if not saved:
            _redirect(
                handler,
                _gmail_redirect(
                    "error",
                    access_token=access_token,
                    reason="supabase_save_failed",
                    error_detail=save_error or "Failed to write token to Supabase",
                    stage="supabase_save",
                ),
            )
            return
    else:
        # Should not reach here — guarded above
        return

    print(f"[gmail_auth] === OAuth callback succeeded (user_id={user_id}) ===")
    _redirect(handler, _gmail_redirect("connected"))


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


def handle_disconnect(handler) -> None:
    user_id = resolve_user_id(handler)
    if not user_id:
        handler._json(401, {"detail": "Sign in required"})
        return
    ok, err = disconnect_user_token(user_id)
    print(f"[gmail_auth] disconnect user_id={user_id} ok={ok} error={err or '(none)'}")
    if not ok:
        handler._json(502, {"detail": err or "Failed to disconnect Gmail"})
        return
    handler._json(200, {"success": True, "connected": False})


def _redirect(handler, url: str) -> None:
    handler.send_response(302)
    handler.send_header("Location", url)
    handler.end_headers()
