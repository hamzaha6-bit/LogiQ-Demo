import bootstrap_path  # noqa: F401

import base64
import json
import logging
import os
import secrets
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow

# Required for OAuth over http://localhost (development only)
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

from env_loader import ENV_FILE, get_env_from_file

logger = logging.getLogger("logiq.gmail")

BACKEND_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BACKEND_DIR / "token.json"
OAUTH_STATE_FILE = BACKEND_DIR / "oauth_state.json"
# Byte-for-byte canonical redirect URI — no trailing slash, http not https, localhost not 127.0.0.1
GMAIL_REDIRECT_URI = "http://localhost:8000/api/auth/gmail/callback"

if GMAIL_REDIRECT_URI != "http://localhost:8000/api/auth/gmail/callback":
    raise RuntimeError("GMAIL_REDIRECT_URI constant corrupted")
if GMAIL_REDIRECT_URI.endswith("/"):
    raise RuntimeError("GMAIL_REDIRECT_URI must not have a trailing slash")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_AUTH_MESSAGE = "Gmail not authorised — visit /api/auth/gmail/connect"


class GmailNotConfigured(Exception):
    """OAuth client secrets or sender email missing/invalid in environment."""


class GmailNotAuthorised(Exception):
    """No valid token.json — user must complete OAuth flow."""


class GmailOAuthCallbackError(Exception):
    """OAuth callback failed — carries a safe user-facing reason."""

    def __init__(self, message: str, user_reason: str = ""):
        super().__init__(message)
        self.user_reason = user_reason or message


_PLACEHOLDER_SECRETS = frozenset(
    {"YOUR_NEW_SECRET_HERE", "your_client_secret", "...", "paste", "changeme"}
)


def _log_redirect_uri(stage: str, uri: Optional[str]) -> None:
    """Log exact redirect_uri bytes for OAuth debugging."""
    value = uri or ""
    canonical = value == GMAIL_REDIRECT_URI
    logger.info(
        "redirect_uri [%s]: %r | len=%d | canonical=%s",
        stage,
        value,
        len(value),
        canonical,
    )
    if value and not canonical:
        logger.error(
            "redirect_uri MISMATCH at [%s] — expected %r, got %r",
            stage,
            GMAIL_REDIRECT_URI,
            value,
        )


def _extract_redirect_uri_from_auth_url(auth_url: str) -> str:
    params = parse_qs(urlparse(auth_url).query)
    values = params.get("redirect_uri") or params.get("redirect_url") or []
    return unquote(values[0]) if values else ""


def _enforce_canonical_redirect_uris(section: Dict[str, Any]) -> None:
    """Replace credentials JSON redirect_uris with the hardcoded constant only."""
    existing = list(section.get("redirect_uris") or [])
    if existing != [GMAIL_REDIRECT_URI]:
        logger.info(
            "GMAIL_CREDENTIALS_JSON redirect_uris in .env: %r — IGNORED, using hardcoded constant",
            existing,
        )
    section["redirect_uris"] = [GMAIL_REDIRECT_URI]


def _token_exchange_redirect_uri() -> str:
    """Redirect URI for token exchange — always the hardcoded constant."""
    return GMAIL_REDIRECT_URI


def _log_connect_redirect_uri_summary(auth_redirect_uri: str) -> None:
    """Print both redirect_uri values when /api/auth/gmail/connect is called."""
    token_redirect_uri = _token_exchange_redirect_uri()
    logger.info("=== /api/auth/gmail/connect — redirect_uri report ===")
    logger.info(
        "Authorization URL redirect_uri (sent to Google now): %r",
        auth_redirect_uri,
    )
    logger.info(
        "Token exchange redirect_uri (used on callback): %r",
        token_redirect_uri,
    )
    logger.info(
        "Hardcoded constant GMAIL_REDIRECT_URI: %r",
        GMAIL_REDIRECT_URI,
    )
    logger.info(
        "Both match constant: %s",
        auth_redirect_uri == token_redirect_uri == GMAIL_REDIRECT_URI,
    )
    logger.info(
        "Source: HARDCODED GMAIL_REDIRECT_URI — never read from GMAIL_CREDENTIALS_JSON redirect_uris"
    )
    logger.info("=== end redirect_uri report ===")


def get_sender_email() -> str:
    return get_env_from_file("GMAIL_SENDER_EMAIL")


def _get_credentials_raw() -> str:
    return get_env_from_file("GMAIL_CREDENTIALS_JSON")


def _parse_credentials_json() -> Dict[str, Any]:
    raw = _get_credentials_raw()
    if not raw:
        raise GmailNotConfigured(
            "GMAIL_CREDENTIALS_JSON is empty — paste your Google OAuth client secrets JSON into backend/.env"
        )
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GmailNotConfigured(
            f"GMAIL_CREDENTIALS_JSON is invalid JSON: {exc}. "
            "Paste the full downloaded credentials.json on a single line."
        ) from exc

    if not isinstance(config, dict):
        raise GmailNotConfigured("GMAIL_CREDENTIALS_JSON must be a JSON object")

    section = config.get("web") or config.get("installed")
    section_name = "web" if config.get("web") else "installed" if config.get("installed") else None
    if not section:
        raise GmailNotConfigured(
            "GMAIL_CREDENTIALS_JSON must contain a 'web' or 'installed' key "
            "(download OAuth client JSON from Google Cloud Console)"
        )

    client_id = (section.get("client_id") or "").strip()
    client_secret = (section.get("client_secret") or "").strip()
    if not client_id:
        raise GmailNotConfigured("GMAIL_CREDENTIALS_JSON missing client_id")
    if not client_secret:
        raise GmailNotConfigured("GMAIL_CREDENTIALS_JSON missing client_secret")
    if client_secret in _PLACEHOLDER_SECRETS or "YOUR_" in client_secret.upper():
        raise GmailNotConfigured(
            "GMAIL_CREDENTIALS_JSON client_secret is still a placeholder — "
            "open Google Cloud Console → APIs & Services → Credentials → your OAuth client, "
            "copy the real Client secret into backend/.env, then restart the server"
        )

    if "redirect_uris" not in section:
        raise GmailNotConfigured(
            "GMAIL_CREDENTIALS_JSON missing redirect_uris — "
            "use the OAuth client JSON downloaded from Google Cloud Console"
        )

    _enforce_canonical_redirect_uris(section)
    config[section_name] = section

    logger.debug(
        "Gmail client config OK — client_id=%s… redirect_uris=%r",
        client_id[:12],
        section["redirect_uris"],
    )
    return config


def is_gmail_configured() -> bool:
    if not get_sender_email():
        return False
    try:
        _parse_credentials_json()
        return True
    except GmailNotConfigured:
        return False


def log_gmail_startup_status() -> None:
    sender = get_sender_email()
    raw = _get_credentials_raw()
    logger.info(
        "Gmail env load: GMAIL_SENDER_EMAIL=%s (%d chars), GMAIL_CREDENTIALS_JSON=%s (%d chars), .env=%s",
        "SET" if sender else "EMPTY",
        len(sender),
        "SET" if raw else "EMPTY",
        len(raw),
        ENV_FILE.resolve(),
    )
    if not sender and not raw:
        logger.warning(
            "Gmail not configured — set GMAIL_SENDER_EMAIL and GMAIL_CREDENTIALS_JSON in backend/.env"
        )
        return
    if not sender:
        logger.error("GMAIL_SENDER_EMAIL is empty")
        return
    try:
        _parse_credentials_json()
        logger.info("Gmail OAuth client configured for %s", sender)
        _log_redirect_uri("startup-canonical-constant", GMAIL_REDIRECT_URI)
        logger.info("Gmail authorised: %s", is_gmail_authorised())
    except GmailNotConfigured as exc:
        logger.error("Gmail credentials invalid: %s", exc)


def _encode_oauth_state(user_id: Optional[str]) -> str:
    payload = {"user_id": user_id or "", "nonce": secrets.token_urlsafe(16)}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return raw.rstrip("=")


def decode_oauth_state(state: str) -> Optional[str]:
    if not state:
        return None
    try:
        pad = "=" * (-len(state) % 4)
        data = json.loads(base64.urlsafe_b64decode(state + pad))
        uid = (data.get("user_id") or "").strip()
        return uid or None
    except Exception:
        return None


def _token_data_from_file() -> Optional[Dict[str, Any]]:
    if not TOKEN_FILE.exists() or TOKEN_FILE.stat().st_size == 0:
        return None
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_user_token_data(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        from supabase_client import get_url, is_configured, rest_headers

        if not is_configured() or not user_id:
            return None
        import httpx

        url = f"{get_url()}/rest/v1/user_integrations"
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                url,
                headers=rest_headers(),
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
    except Exception as exc:
        logger.warning("Could not load Gmail token for user %s: %s", user_id, exc)
    return None


def _save_user_token_data(user_id: str, token_data: Dict[str, Any]) -> bool:
    try:
        from supabase_client import get_url, is_configured, rest_headers

        if not is_configured() or not user_id:
            return False
        import httpx

        url = f"{get_url()}/rest/v1/user_integrations"
        headers = {**rest_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                url,
                headers=headers,
                params={"on_conflict": "user_id,integration"},
                json={"user_id": user_id, "integration": "gmail", "token_data": token_data},
            )
            return resp.status_code < 400
    except Exception as exc:
        logger.exception("Failed to save Gmail token for user %s", user_id)
        return False


def _scopes_in_token_data(data: Optional[Dict[str, Any]]) -> list:
    if not data:
        return []
    return data.get("scopes") or []


def has_sheets_scope(user_id: Optional[str] = None) -> bool:
    """True if user's token includes Google Sheets read or write scope."""
    data = _load_user_token_data(user_id) if user_id else None
    if not data:
        data = _token_data_from_file()
    scopes = _scopes_in_token_data(data)
    return SHEETS_SCOPE in scopes or SHEETS_READONLY_SCOPE in scopes


def has_calendar_scope(user_id: Optional[str] = None) -> bool:
    data = _load_user_token_data(user_id) if user_id else None
    if not data:
        data = _token_data_from_file()
    scopes = _scopes_in_token_data(data)
    return CALENDAR_EVENTS_SCOPE in scopes or "calendar.readonly" in " ".join(scopes)


def is_gmail_authorised(user_id: Optional[str] = None) -> bool:
    try:
        get_credentials(user_id)
        return True
    except (GmailNotAuthorised, GmailNotConfigured):
        return False


def get_frontend_redirect() -> str:
    return (
        os.getenv("FRONTEND_URL")
        or os.getenv("OAUTH_REDIRECT_BASE")
        or "http://localhost:8000"
    ).rstrip("/")


def _build_flow() -> Flow:
    # redirect_uri is always the hardcoded constant — passed explicitly, not taken from JSON array
    flow = Flow.from_client_config(
        _parse_credentials_json(),
        scopes=GMAIL_SCOPES,
        redirect_uri=GMAIL_REDIRECT_URI,
    )
    flow.oauth2session.redirect_uri = GMAIL_REDIRECT_URI
    if getattr(flow, "redirect_uri", None) not in (None, GMAIL_REDIRECT_URI):
        logger.warning(
            "Flow.redirect_uri was %r — forcing %r",
            flow.redirect_uri,
            GMAIL_REDIRECT_URI,
        )
        flow.redirect_uri = GMAIL_REDIRECT_URI
    return flow


def _save_token(creds: Credentials) -> None:
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Gmail token saved to %s", TOKEN_FILE)


def _save_oauth_state(state: str) -> None:
    OAUTH_STATE_FILE.write_text(json.dumps({"state": state}), encoding="utf-8")


def _restore_oauth_state(flow: Flow) -> None:
    if not OAUTH_STATE_FILE.exists():
        logger.warning("No saved OAuth state — token exchange may fail if state validation is enforced")
        return
    try:
        data = json.loads(OAUTH_STATE_FILE.read_text(encoding="utf-8"))
        state = data.get("state")
        if state:
            flow.oauth2session.state = state
            logger.debug("Restored OAuth state for token exchange")
    except Exception as exc:
        logger.exception("Failed to restore OAuth state from %s", OAUTH_STATE_FILE)


def _clear_oauth_state() -> None:
    try:
        OAUTH_STATE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove OAuth state file: %s", exc)


def _log_google_token_error(exc: Exception) -> None:
    """Log everything Google/oauthlib returns on token exchange failure."""
    logger.error("Google token exchange error type: %s", type(exc).__name__)
    logger.error("Google token exchange error message: %s", exc)
    for attr in ("error", "description", "uri", "state", "status_code", "error_uri"):
        val = getattr(exc, attr, None)
        if val:
            logger.error("Google OAuth error field %s: %s", attr, val)
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        body = getattr(response, "text", None) or getattr(response, "data", None)
        logger.error("Google HTTP response status: %s", status)
        logger.error("Google HTTP response body: %s", body)


def _oauth_user_reason(exc: Exception) -> str:
    error_code = getattr(exc, "error", None) or type(exc).__name__
    description = getattr(exc, "description", None) or str(exc)
    if "invalid_client" in str(error_code).lower() or "invalid_client" in description.lower():
        return "invalid_client_secret"
    if "redirect_uri" in description.lower():
        return "redirect_uri_mismatch"
    if "state" in description.lower():
        return "state_mismatch"
    return str(error_code)[:80]


def get_gmail_redirect_uri() -> str:
    """Return the canonical Gmail OAuth redirect URI (single source of truth)."""
    return GMAIL_REDIRECT_URI


def get_authorization_url(user_id: Optional[str] = None) -> str:
    flow = _build_flow()
    flow.oauth2session.redirect_uri = GMAIL_REDIRECT_URI

    custom_state = _encode_oauth_state(user_id)
    auth_url, _google_state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=custom_state,
    )
    _save_oauth_state(custom_state)
    if user_id:
        OAUTH_STATE_FILE.write_text(
            json.dumps({"state": custom_state, "user_id": user_id}), encoding="utf-8"
        )

    auth_redirect_uri = _extract_redirect_uri_from_auth_url(auth_url)
    if auth_redirect_uri != GMAIL_REDIRECT_URI:
        raise GmailNotConfigured(
            f"Auth URL redirect_uri mismatch — Google would receive {auth_redirect_uri!r} "
            f"but hardcoded constant is {GMAIL_REDIRECT_URI!r}"
        )

    _log_connect_redirect_uri_summary(auth_redirect_uri)

    logger.info("Gmail OAuth URL generated (user_id=%s, state=%s…)", user_id or "none", (custom_state or "")[:8])
    logger.info("Scopes: %s", GMAIL_SCOPES)
    return auth_url


def exchange_code(code: str, state: Optional[str] = None) -> Credentials:
    """Exchange authorization code for tokens."""
    token_redirect_uri = _token_exchange_redirect_uri()
    logger.info("=== Gmail token exchange ===")
    logger.info("Token exchange redirect_uri (hardcoded): %r", token_redirect_uri)
    logger.info("authorization code length: %d", len(code))

    flow = _build_flow()
    logger.info(
        "Token exchange using Flow redirect_uri: %r",
        flow.oauth2session.redirect_uri,
    )

    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        _log_google_token_error(exc)
        logger.exception("Gmail OAuth token exchange failed — full traceback above")
        raise
    finally:
        _clear_oauth_state()

    if not flow.credentials or not flow.credentials.token:
        raise RuntimeError("Token exchange returned empty credentials")
    return flow.credentials


def persist_oauth_credentials(creds: Credentials, user_id: Optional[str] = None) -> None:
    token_data = json.loads(creds.to_json())
    _save_token(creds)
    if user_id:
        saved = _save_user_token_data(user_id, token_data)
        logger.info("Gmail token saved for user %s (supabase=%s)", user_id, saved)
    logger.info("Gmail OAuth complete — token saved to %s", TOKEN_FILE)


def handle_oauth_callback(full_url: str, query_params: Dict[str, str]) -> None:
    """Process the OAuth callback — logs all params and exchanges the code for tokens."""
    code = (query_params.get("code") or "").strip()
    error = (query_params.get("error") or "").strip()
    state = (query_params.get("state") or "").strip()
    error_description = (query_params.get("error_description") or "").strip()

    logger.info("=== Gmail OAuth callback received ===")
    _log_redirect_uri("callback-canonical-constant", GMAIL_REDIRECT_URI)
    logger.info("Full callback URL: %s", full_url)
    logger.info("code parameter: %s", f"{code[:24]}… ({len(code)} chars)" if len(code) > 24 else code or "(missing)")
    logger.info("error parameter: %s", error or "(none)")
    logger.info("state parameter: %s", state or "(none)")
    logger.info("error_description parameter: %s", error_description or "(none)")

    parsed = urlparse(full_url)
    incoming_base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    _log_redirect_uri("callback-incoming-request-base", incoming_base)

    if incoming_base != GMAIL_REDIRECT_URI:
        logger.warning(
            "Incoming callback URL base %r differs from canonical %r — "
            "token exchange uses canonical redirect_uri only; visit http://localhost:8000 not 127.0.0.1",
            incoming_base,
            GMAIL_REDIRECT_URI,
        )
    if parsed.scheme == "https":
        logger.error("Callback arrived over https — canonical redirect_uri uses http")
    if parsed.path.rstrip("/") != "/api/auth/gmail/callback":
        logger.error(
            "Callback path %r is not /api/auth/gmail/callback",
            parsed.path,
        )

    if error:
        msg = f"Google OAuth error: {error}"
        if error_description:
            msg = f"{msg} — {error_description}"
        logger.error(msg)
        raise GmailOAuthCallbackError(msg, user_reason=error)

    if not code:
        logger.error("Gmail OAuth callback missing authorization code")
        raise GmailOAuthCallbackError("Missing authorization code", user_reason="missing_code")

    user_id = decode_oauth_state(state)
    if not user_id and OAUTH_STATE_FILE.exists():
        try:
            saved = json.loads(OAUTH_STATE_FILE.read_text(encoding="utf-8"))
            user_id = saved.get("user_id") or decode_oauth_state(saved.get("state", ""))
        except Exception:
            pass

    try:
        creds = exchange_code(code, state=state or None)
        persist_oauth_credentials(creds, user_id=user_id)
    except GmailNotConfigured:
        raise
    except Exception as exc:
        logger.exception("Gmail OAuth callback handler failed — full traceback above")
        _log_google_token_error(exc)
        raise GmailOAuthCallbackError(
            str(exc),
            user_reason=_oauth_user_reason(exc),
        ) from exc

    logger.info("=== Gmail OAuth callback succeeded (user_id=%s) ===", user_id or "legacy")


def get_credentials(user_id: Optional[str] = None) -> Credentials:
    if not is_gmail_configured():
        raise GmailNotConfigured(
            "Gmail not configured — set GMAIL_SENDER_EMAIL and GMAIL_CREDENTIALS_JSON in backend/.env"
        )

    token_data = _load_user_token_data(user_id) if user_id else None
    if not token_data:
        token_data = _token_data_from_file()

    if not token_data:
        msg = "Connect your Gmail first" if user_id else GMAIL_AUTH_MESSAGE
        raise GmailNotAuthorised(msg)

    try:
        creds = Credentials.from_authorized_user_info(token_data, GMAIL_SCOPES)
    except Exception as exc:
        logger.exception("Failed to load Gmail credentials")
        raise GmailNotAuthorised("Connect your Gmail first" if user_id else GMAIL_AUTH_MESSAGE) from exc

    if creds.expired:
        if creds.refresh_token:
            logger.info("Gmail token expired — refreshing")
            try:
                creds.refresh(Request())
                persist_oauth_credentials(creds, user_id=user_id)
            except Exception as exc:
                logger.exception("Gmail token refresh failed")
                raise GmailNotAuthorised("Connect your Gmail first" if user_id else GMAIL_AUTH_MESSAGE) from exc
        else:
            raise GmailNotAuthorised("Connect your Gmail first" if user_id else GMAIL_AUTH_MESSAGE)

    return creds


def check_gmail_health(user_id: Optional[str] = None) -> Dict[str, Any]:
    """Validate token, refresh if needed, probe Gmail profile."""
    result: Dict[str, Any] = {
        "connected": False,
        "healthy": False,
        "email": "",
        "sheets_scope": False,
        "calendar_scope": False,
        "error": "",
    }
    if user_id:
        token_data = _load_user_token_data(user_id)
    else:
        token_data = _token_data_from_file()
    if not token_data:
        result["error"] = "not_connected"
        return result
    result["connected"] = True
    result["sheets_scope"] = has_sheets_scope(user_id)
    result["calendar_scope"] = has_calendar_scope(user_id)
    try:
        creds = get_credentials(user_id)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
        result["email"] = profile.get("emailAddress", "")
        result["healthy"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def send_email(
    to: str, subject: str, body: str, from_name: str = "", user_id: Optional[str] = None
) -> Tuple[bool, str]:
    health = check_gmail_health(user_id) if user_id else {"healthy": is_gmail_authorised()}
    if user_id and not health.get("healthy"):
        raise GmailNotAuthorised(health.get("error") or "Connect your Gmail first")
    sender = health.get("email") if user_id else get_sender_email()
    if not sender:
        sender = get_sender_email()
    creds = get_credentials(user_id)

    from_header = f'"{from_name}" <{sender}>' if from_name else sender
    message = MIMEText(body, "plain", "utf-8")
    message["to"] = to
    message["from"] = from_header
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        result = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        logger.info("Gmail sent to %s — message_id=%s", to, result.get("id"))
        return True, result.get("id", "sent")
    except Exception as exc:
        logger.exception("Gmail send failed to %s", to)
        return False, str(exc)
