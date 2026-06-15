"""Shared logic for Supabase Before User Created auth hook."""
from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict

from standardwebhooks.webhooks import Webhook, WebhookVerificationError

from gmail_send import is_gmail_configured, send_platform_email

WELCOME_SUBJECT = "Welcome to LogiQ — you're in."
WELCOME_FROM_NAME = "Hamza at LogiQ"


def _hook_secret() -> str:
    raw = (os.environ.get("SUPABASE_AUTH_HOOK_SECRET") or "").strip()
    if raw.startswith("v1,whsec_"):
        return raw[len("v1,whsec_") :]
    if raw.startswith("whsec_"):
        return raw[len("whsec_") :]
    return raw


def _first_name(user: dict) -> str:
    meta = user.get("user_metadata") or {}
    name = (meta.get("name") or meta.get("full_name") or "").strip()
    if name:
        return name.split()[0]
    email = (user.get("email") or "").strip()
    if email and "@" in email:
        return email.split("@")[0]
    return "there"


def _welcome_body(first_name: str) -> str:
    return f"""Hi {first_name},

Welcome to LogiQ. You're one of the first businesses to get access to the platform and we're glad you're here.

Here's what to do next:
1. Connect your Gmail — takes 30 seconds and unlocks Aria and Nova immediately
2. Tell Blueprint AI what you want to automate — describe it in plain English and it builds the workflow
3. Review and approve your first workflow — nothing runs until you say so

If you get stuck or have any questions, reply to this email directly. I read every one.

Hamza
Founder, LogiQ
logiqops.co.uk

P.S. You're on early access pricing. That rate is locked in for as long as you stay a customer.
"""


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    handler.send_response(status)
    handler.send_header("Content-type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(payload).encode())


def is_user_created_hook_path(path: str) -> bool:
    normalized = (path or "").rstrip("/").lower()
    return normalized.endswith("/hook/user-created") or normalized.endswith("/hooks/user-created")


def handle_user_created_hook(handler: BaseHTTPRequestHandler) -> None:
    try:
        length = int(handler.headers.get("Content-Length", 0))
        raw = handler.rfile.read(length) if length else b""
        payload_text = raw.decode("utf-8")
    except Exception as exc:
        json_response(handler, 400, {"error": {"message": f"Invalid request body: {exc}"}})
        return

    secret = _hook_secret()
    if not secret:
        print("[auth_hook] SUPABASE_AUTH_HOOK_SECRET not set")
        json_response(handler, 200, {})
        return

    headers = {
        "webhook-id": handler.headers.get("webhook-id", ""),
        "webhook-timestamp": handler.headers.get("webhook-timestamp", ""),
        "webhook-signature": handler.headers.get("webhook-signature", ""),
    }

    try:
        wh = Webhook(secret)
        event = wh.verify(payload_text, headers)
    except WebhookVerificationError as exc:
        print(f"[auth_hook] Webhook verification failed: {exc}")
        json_response(handler, 401, {"error": {"message": "Invalid webhook signature"}})
        return
    except Exception as exc:
        print(f"[auth_hook] Webhook verify error: {exc}")
        traceback.print_exc()
        json_response(handler, 400, {"error": {"message": "Invalid request format"}})
        return

    if isinstance(event, str):
        try:
            event = json.loads(event)
        except json.JSONDecodeError:
            event = {}

    user = event.get("user") or {}
    email = (user.get("email") or "").strip()
    hook_name = (event.get("metadata") or {}).get("name", "")

    if not email:
        print("[auth_hook] No email in hook payload — skipping welcome email")
        json_response(handler, 200, {})
        return

    if hook_name and hook_name not in ("before-user-created", "on_auth_user_created"):
        print(f"[auth_hook] Unhandled hook type: {hook_name}")

    if not is_gmail_configured():
        print("[auth_hook] Gmail not configured — signup proceeds without welcome email")
        json_response(handler, 200, {})
        return

    first_name = _first_name(user)
    try:
        ok, detail = send_platform_email(
            to=email,
            subject=WELCOME_SUBJECT,
            body=_welcome_body(first_name),
            from_name=WELCOME_FROM_NAME,
        )
        if ok:
            print(f"[auth_hook] Welcome email sent to {email} (id={detail})")
        else:
            print(f"[auth_hook] Welcome email failed for {email}: {detail}")
    except Exception as exc:
        print(f"[auth_hook] Welcome email error for {email}: {exc}")
        traceback.print_exc()

    json_response(handler, 200, {})
