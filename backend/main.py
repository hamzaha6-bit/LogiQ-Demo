import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import logging
from pathlib import Path

from env_loader import ENV_FILE, bootstrap_env, debug_env_status

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("logiq")

bootstrap_env()

import asyncio
import json
import re
import traceback
from typing import Any, Dict, List, Optional

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from gmail_service import (
    GmailNotAuthorised,
    GmailNotConfigured,
    GmailOAuthCallbackError,
    exchange_code,
    get_authorization_url,
    get_frontend_redirect,
    get_gmail_redirect_uri,
    handle_oauth_callback,
    has_sheets_scope,
    is_gmail_authorised,
    is_gmail_configured,
    log_gmail_startup_status,
    send_email,
)
import auth_service
import audit
import billing
import usage
from auth_service import AuthError, AuthNotConfigured
from supabase_client import env_status, is_configured as supabase_backend_configured, is_url_set
import sheets_service
from sheets_service import SheetsError, SheetsScopeMissing
from integrations import hubspot, xero
from env_loader import BACKEND_DIR, ROOT_DIR
from rate_limit import is_rate_limited

app = FastAPI(title="LogiQ API", version="1.0.0")


def _api_json_error(status_code: int, detail: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


@app.exception_handler(RequestValidationError)
async def api_validation_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/"):
        return _api_json_error(422, exc.errors())
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(StarletteHTTPException)
async def api_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        detail = exc.detail if isinstance(exc.detail, (str, list, dict)) else str(exc.detail)
        return _api_json_error(exc.status_code, detail)
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def api_unhandled_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    logger.exception("Unhandled error on %s", request.url.path)
    if request.url.path.startswith("/api/"):
        return _api_json_error(500, str(exc) or "Internal server error")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MODEL = "claude-sonnet-4-5"


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        ip = get_client_ip(request)
        if is_rate_limited(ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Maximum 30 requests per minute."},
            )
    return await call_next(request)


PROTECTED_PREFIXES = ("/api/agent/", "/api/send/")


async def resolve_user(request: Request) -> Optional[Dict[str, Any]]:
    auth = request.headers.get("Authorization", "")
    token = request.query_params.get("token", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    if not token:
        return None
    return await auth_service.get_user_from_token(token)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    request.state.user = None
    request.state.user_id = None
    user = await resolve_user(request)
    if user:
        request.state.user = user
        request.state.user_id = user["id"]

    path = request.url.path
    if supabase_backend_configured() and any(path.startswith(p) for p in PROTECTED_PREFIXES):
        if not user:
            return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    return await call_next(request)


def get_request_user_id(request: Request) -> Optional[str]:
    return getattr(request.state, "user_id", None)


def get_anthropic_api_key() -> str:
    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        hint = (
            f"Anthropic API key not configured. "
            f"Add ANTHROPIC_API_KEY=sk-ant-... to {ENV_FILE} "
            f"or export it as an environment variable, then restart the server."
        )
        if not ENV_FILE.exists():
            hint += f" ({ENV_FILE} does not exist)"
        raise HTTPException(status_code=503, detail=hint)
    return key


def get_anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=get_anthropic_api_key())


# ─── Models ───────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    system: str = ""
    max_tokens: int = 1200


class ChatResponse(BaseModel):
    content: str


class AgentInfo(BaseModel):
    name: str
    system_prompt: str


class AgentItem(BaseModel):
    item_id: str
    data: Dict[str, Any]
    history: str = ""


class AgentSettings(BaseModel):
    business: str = ""
    tone: str = ""
    cta: str = ""
    calendly_link: str = ""
    from_name: str = ""


class AgentRunRequest(BaseModel):
    agent: AgentInfo
    items: List[AgentItem]
    settings: AgentSettings = Field(default_factory=AgentSettings)


class GmailSendRequest(BaseModel):
    to: str
    subject: str
    body: str
    from_name: str = ""


class GmailSendResponse(BaseModel):
    success: bool
    message_id: str
    configured: bool = True


class SheetsConnectRequest(BaseModel):
    url: str


class HubSpotContactRequest(BaseModel):
    name: str
    email: str = ""
    company: str = ""
    status: str = ""


class SignupRequest(BaseModel):
    name: str
    email: str
    password: str = Field(min_length=6)


class LoginRequest(BaseModel):
    email: str
    password: str


class CheckoutRequest(BaseModel):
    plan: str


class AuditEventRequest(BaseModel):
    agent: str
    action_type: str
    item_id: str = ""
    recipient: str = ""
    subject: str = ""
    status: str = "completed"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProfileUpdateRequest(BaseModel):
    onboarding_complete: Optional[bool] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_agent_json(text: str) -> Dict[str, Any]:
    clean = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\{[\s\S]*\}", clean)
    if not match:
        raise ValueError("Could not parse JSON from response")
    return json.loads(match.group(0))


def build_system_prompt(base: str, settings: AgentSettings, agent_name: str = "") -> str:
    parts = [base]
    if settings.tone:
        parts.append(f"Tone: {settings.tone}.")
    if settings.cta:
        parts.append(f"CTA: {settings.cta}.")
    if settings.business:
        parts.append(f"Business: {settings.business}.")
    calendly = (settings.calendly_link or os.getenv("CALENDLY_LINK") or "").strip()
    if agent_name == "Nova" and calendly:
        parts.append(
            f"If the message indicates interest in a meeting or call, append this Calendly booking link at the end of the response: {calendly}"
        )
    return "\n".join(parts)


def sse_event(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_frontend():
    index = ROOT_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"message": "LogiQ API — index.html not found"}


@app.get("/api/debug/env")
async def debug_env():
    return debug_env_status()


@app.get("/api/config")
async def public_config():
    return {
        **auth_service.public_config(),
        "stripe_configured": billing.is_configured(),
    }


@app.get("/api/health")
async def health():
    key_ok = bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())
    supabase_env = env_status()
    return {
        "status": "ok",
        "runtime": "vercel" if os.getenv("VERCEL") else "local",
        "anthropic_configured": key_ok,
        "supabase_configured": is_url_set(),
        "supabase_backend_configured": supabase_backend_configured(),
        "supabase_env": supabase_env,
        "stripe_configured": billing.is_configured(),
        "integrations": {
            "gmail": is_gmail_configured(),
            "gmail_authorised": is_gmail_authorised(),
            "sheets": sheets_service.is_available(),
            "sheets_scope": has_sheets_scope(),
            "xero": xero.is_configured(),
            "hubspot": hubspot.is_configured(),
            "calendly": bool((os.getenv("CALENDLY_LINK") or "").strip()),
        },
    }


# ─── Auth ─────────────────────────────────────────────────────────────────────

@app.post("/api/auth/signup")
async def auth_signup(req: SignupRequest):
    try:
        return await auth_service.signup(req.name, req.email, req.password)
    except AuthNotConfigured as exc:
        return _api_json_error(503, str(exc))
    except AuthError as exc:
        return _api_json_error(exc.status, str(exc))
    except Exception as exc:
        traceback.print_exc()
        logger.exception("Unhandled error in /api/auth/signup")
        return _api_json_error(500, str(exc) or "Signup failed")


@app.post("/api/auth/login")
async def auth_login(req: LoginRequest):
    try:
        return await auth_service.login(req.email, req.password)
    except AuthNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    await auth_service.logout(token)
    return {"success": True}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if token:
            user = await auth_service.get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    profile = await auth_service.get_profile(user["id"])
    return {**user, **profile}


@app.patch("/api/auth/profile")
async def auth_profile_update(req: ProfileUpdateRequest, request: Request):
    user_id = get_request_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if updates:
        await auth_service.update_profile(user_id, updates)
    profile = await auth_service.get_profile(user_id)
    return profile


# ─── Billing ──────────────────────────────────────────────────────────────────

@app.post("/api/billing/create-checkout")
async def billing_checkout(req: CheckoutRequest, request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not billing.is_configured():
        raise HTTPException(status_code=503, detail="Stripe not configured")
    base = get_frontend_redirect()
    try:
        return await billing.create_checkout(
            user["id"],
            user.get("email", ""),
            req.plan,
            f"{base}/?payment=success",
            f"{base}/?payment=cancelled",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/billing/webhook")
async def billing_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    result = await billing.handle_webhook(payload, sig)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Webhook failed"))
    return result


@app.get("/api/billing/status")
async def billing_status(request: Request):
    user_id = get_request_user_id(request)
    if not user_id:
        return {
            "plan": "starter",
            "plan_name": "Starter",
            "limits": billing.get_plan_limits("starter"),
            "usage": {"api_calls_today": 0, "emails_sent_today": 0, "actions_this_month": 0},
            "percentages": {"api_calls": 0, "emails": 0, "actions": 0},
            "stripe_configured": billing.is_configured(),
        }
    return await billing.get_billing_status(user_id)


# ─── Audit ────────────────────────────────────────────────────────────────────

@app.get("/api/audit/log")
async def audit_log_list(request: Request, limit: int = 20, agent: Optional[str] = None):
    user_id = get_request_user_id(request)
    rows = await audit.get_log(user_id, limit=min(limit, 100), agent=agent)
    return {"entries": rows}


@app.post("/api/audit/event")
async def audit_log_event(req: AuditEventRequest, request: Request):
    user_id = get_request_user_id(request)
    await audit.log_event(
        user_id,
        req.agent,
        req.action_type,
        item_id=req.item_id,
        recipient=req.recipient,
        subject=req.subject,
        status=req.status,
        metadata=req.metadata,
    )
    if user_id:
        await usage.record_action(user_id)
    return {"success": True}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    user_id = get_request_user_id(request)
    if user_id:
        ok, msg = await usage.check_api_limit(user_id)
        if not ok:
            raise HTTPException(status_code=429, detail=msg)
    client = get_anthropic_client()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=req.max_tokens,
            system=req.system,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
        )
        content = response.content[0].text if response.content else ""
        if user_id:
            await usage.record_api_call(user_id)
        return ChatResponse(content=content)
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/agent/run")
async def agent_run(req: AgentRunRequest, request: Request):
    user_id = get_request_user_id(request)
    if user_id:
        ok, msg = await usage.check_api_limit(user_id)
        if not ok:
            raise HTTPException(status_code=429, detail=msg)
    client = get_anthropic_client()
    total = len(req.items)
    system = build_system_prompt(req.agent.system_prompt, req.settings, req.agent.name)

    async def stream():
        queued = 0
        yield sse_event("start", {"total": total, "agent": req.agent.name})

        for i, item in enumerate(req.items):
            yield sse_event("progress", {"current": i + 1, "total": total})

            user_content = (
                f"Item data:\n{json.dumps(item.data)}\n\nHistory:\n{item.history or 'No prior actions.'}"
            )
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=1200,
                    system=system,
                    messages=[{"role": "user", "content": user_content}],
                )
                text = response.content[0].text if response.content else ""
                result = parse_agent_json(text)
                action = result.get("action", "review")

                if action not in ("wait", "none"):
                    queued += 1
                    if user_id:
                        await usage.record_api_call(user_id)
                        await usage.record_action(user_id)
                        await audit.log_event(
                            user_id,
                            req.agent.name,
                            "generate",
                            item_id=item.item_id,
                            subject=result.get("subject", ""),
                            status="queued",
                            metadata={"action": action},
                        )
                    yield sse_event(
                        "result",
                        {
                            "item_id": item.item_id,
                            "reasoning": result.get("reasoning", ""),
                            "action": action,
                            "subject": result.get("subject", ""),
                            "body": result.get("body", ""),
                        },
                    )
            except Exception as exc:
                yield sse_event("error", {"index": i + 1, "message": str(exc)})

        yield sse_event("done", {"total": total, "queued": queued})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/send/gmail", response_model=GmailSendResponse)
async def send_gmail(req: GmailSendRequest, request: Request):
    user_id = get_request_user_id(request)
    if user_id:
        ok, msg = await usage.check_email_limit(user_id)
        if not ok:
            raise HTTPException(status_code=429, detail=msg)
    try:
        success, message_id = send_email(req.to, req.subject, req.body, req.from_name, user_id=user_id)
    except GmailNotAuthorised as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except GmailNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not success:
        logger.error("Gmail send failed: %s", message_id)
        raise HTTPException(status_code=502, detail=message_id)
    if user_id:
        await usage.record_email_sent(user_id)
        await audit.log_event(
            user_id,
            "Gmail",
            "send",
            recipient=req.to,
            subject=req.subject,
            status="sent",
        )
    return GmailSendResponse(success=True, message_id=message_id, configured=True)


@app.get("/api/auth/gmail/connect")
async def gmail_connect(request: Request, token: Optional[str] = None):
    user_id = get_request_user_id(request)
    if not user_id and token:
        user = await auth_service.get_user_from_token(token)
        user_id = user["id"] if user else None
    try:
        if not is_gmail_configured():
            raise GmailNotConfigured(
                "Gmail not configured — set GMAIL_SENDER_EMAIL and GMAIL_CREDENTIALS_JSON in backend/.env"
            )
        url = get_authorization_url(user_id=user_id)
        logger.info("Redirecting to Google OAuth (user_id=%s): %s", user_id, url[:80] + "…")
        return RedirectResponse(url)
    except GmailNotConfigured as exc:
        logger.error("Gmail connect failed (not configured): %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Gmail connect failed")
        raise HTTPException(status_code=500, detail=f"Gmail OAuth error: {exc}") from exc


@app.get("/api/auth/gmail/callback")
async def gmail_callback(request: Request):
    query = dict(request.query_params)
    full_url = str(request.url)
    try:
        handle_oauth_callback(full_url, query)
    except GmailOAuthCallbackError as exc:
        logger.exception("Gmail OAuth callback failed: %s", exc)
        reason = exc.user_reason or "oauth_error"
        return RedirectResponse(
            url=f"{get_frontend_redirect()}/?gmail=error&reason={reason}"
        )
    except GmailNotConfigured as exc:
        logger.exception("Gmail OAuth callback — not configured: %s", exc)
        return RedirectResponse(
            url=f"{get_frontend_redirect()}/?gmail=error&reason=not_configured"
        )
    except Exception as exc:
        logger.exception("Gmail OAuth callback unexpected error")
        reason = getattr(exc, "error", None) or type(exc).__name__
        return RedirectResponse(
            url=f"{get_frontend_redirect()}/?gmail=error&reason={reason}"
        )
    logger.info("Gmail OAuth authorised successfully")
    return RedirectResponse(url=f"{get_frontend_redirect()}/?gmail=connected")


@app.get("/api/auth/gmail/status")
async def gmail_status(request: Request):
    user_id = get_request_user_id(request)
    return {
        "connected": is_gmail_authorised(user_id),
        "configured": is_gmail_configured(),
        "sheets_scope": has_sheets_scope(user_id),
        "redirect_uri": get_gmail_redirect_uri(),
    }


@app.get("/api/integrations/config")
async def integrations_config():
    return {
        "calendly_link": (os.getenv("CALENDLY_LINK") or "").strip(),
        "gmail_configured": is_gmail_configured(),
        "gmail_authorised": is_gmail_authorised(),
        "google_authorised": is_gmail_authorised(),
        "sheets_configured": sheets_service.is_configured(),
        "sheets_available": sheets_service.is_available(),
        "sheets_scope": has_sheets_scope(),
        "xero_configured": xero.is_configured(),
        "hubspot_configured": hubspot.is_configured(),
    }


@app.post("/api/integrations/sheets/connect")
async def sheets_connect(req: SheetsConnectRequest, request: Request):
    user_id = get_request_user_id(request)
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Sheet URL is required")
    try:
        result = sheets_service.connect(url, user_id=user_id)
        return result
    except SheetsScopeMissing as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except GmailNotAuthorised as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except GmailNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SheetsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Sheets connect failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/integrations/sheets/read")
async def sheets_read(url: str, request: Request):
    user_id = get_request_user_id(request)
    if not url.strip():
        raise HTTPException(status_code=400, detail="url query parameter is required")
    try:
        rows, columns = sheets_service.read_sheet_with_columns(url.strip(), user_id=user_id)
        return {"success": True, "rows": rows, "row_count": len(rows), "columns": columns}
    except SheetsScopeMissing as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except GmailNotAuthorised as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except GmailNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SheetsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Sheets read failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/integrations/sheets/poll")
async def sheets_poll(url: str, agent: str, request: Request):
    user_id = get_request_user_id(request)
    if not url.strip():
        raise HTTPException(status_code=400, detail="url query parameter is required")
    if not agent.strip():
        raise HTTPException(status_code=400, detail="agent query parameter is required")
    try:
        return sheets_service.poll(url.strip(), agent.strip(), user_id=user_id)
    except SheetsScopeMissing as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except GmailNotAuthorised as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except GmailNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SheetsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Sheets poll failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/integrations/sheets/status")
async def sheets_status():
    return {
        "configured": sheets_service.is_configured(),
        "available": sheets_service.is_available(),
        "sheets_scope": has_sheets_scope(),
        "google_authorised": is_gmail_authorised(),
    }


@app.get("/api/integrations/xero/invoices")
async def xero_invoices():
    if not xero.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Xero not configured — set XERO_CLIENT_ID, XERO_CLIENT_SECRET, XERO_TENANT_ID and XERO_REFRESH_TOKEN",
        )
    invoices, err = await xero.fetch_overdue_invoices()
    if err and not invoices:
        raise HTTPException(status_code=502, detail=err)
    return {"invoices": invoices, "count": len(invoices)}


@app.post("/api/integrations/hubspot/contact")
async def hubspot_contact(req: HubSpotContactRequest):
    if not hubspot.is_configured():
        raise HTTPException(
            status_code=503,
            detail="HubSpot not configured — set HUBSPOT_API_KEY in .env",
        )
    ok, result = await hubspot.upsert_contact(req.name, req.email, req.company, req.status)
    if not ok:
        raise HTTPException(status_code=502, detail=result)
    return {"success": True, "contact_id": result}


@app.on_event("startup")
async def startup_tasks():
    log_gmail_startup_status()
    schema_path = BACKEND_DIR / "schema.sql"
    if schema_path.exists():
        sql = schema_path.read_text(encoding="utf-8")
        print("\n" + "=" * 72)
        print("LOGIQ SUPABASE SCHEMA — run this in Supabase SQL Editor:")
        print("=" * 72)
        print(sql)
        print("=" * 72 + "\n")


# ─── OAuth scaffold ───────────────────────────────────────────────────────────

OAUTH_CONFIG = {
    "hubspot": {
        "auth_url": "https://app.hubspot.com/oauth/authorize",
        "scope": "crm.objects.contacts.read crm.objects.contacts.write",
        "client_id_env": "HUBSPOT_CLIENT_ID",
    },
    "xero": {
        "auth_url": "https://login.xero.com/identity/connect/authorize",
        "scope": "openid profile email accounting.transactions",
        "client_id_env": "XERO_CLIENT_ID",
    },
}


def get_redirect_base() -> str:
    return os.getenv("OAUTH_REDIRECT_BASE", "http://localhost:8000")


async def store_oauth_token(integration: str, token_data: Dict[str, Any]) -> bool:
    """Scaffold: persist OAuth tokens to Supabase when configured."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return False
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            await client.post(
                f"{url}/rest/v1/oauth_tokens",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json={
                    "integration": integration,
                    "access_token": token_data.get("access_token", ""),
                    "refresh_token": token_data.get("refresh_token", ""),
                    "expires_at": token_data.get("expires_at"),
                },
            )
        return True
    except Exception:
        return False


@app.get("/api/auth/{integration}/connect")
async def oauth_connect(integration: str, request: Request):
    if integration not in OAUTH_CONFIG:
        raise HTTPException(status_code=404, detail="Unknown integration")

    cfg = OAUTH_CONFIG[integration]
    client_id = os.getenv(cfg["client_id_env"], "")
    if not client_id:
        raise HTTPException(
            status_code=503,
            detail=f"{integration} OAuth not configured. Set {cfg['client_id_env']}.",
        )

    redirect_uri = f"{get_redirect_base()}/api/auth/{integration}/callback"
    state = f"logiq_{integration}_{get_client_ip(request)}"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": cfg["scope"],
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return {"url": f"{cfg['auth_url']}?{query}"}


@app.get("/api/auth/{integration}/callback")
async def oauth_callback(integration: str, code: Optional[str] = None, error: Optional[str] = None):
    if integration not in OAUTH_CONFIG:
        raise HTTPException(status_code=404, detail="Unknown integration")
    if error:
        return RedirectResponse(url=f"/?oauth_error={error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    token_data = {
        "access_token": f"scaffold_token_{integration}_{code[:8]}",
        "refresh_token": f"scaffold_refresh_{integration}",
        "expires_at": None,
    }
    stored = await store_oauth_token(integration, token_data)
    status = "connected" if stored else "scaffold_ok"
    return RedirectResponse(url=f"/?oauth={integration}&status={status}")


# Vercel serverless handler (also used when backend/main.py is the build entry)
try:
    from mangum import Mangum

    handler = Mangum(app, lifespan="off")
except ImportError:
    handler = app
