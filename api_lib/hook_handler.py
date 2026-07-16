"""Shared logic for Supabase Before User Created auth hook."""
from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Tuple

from gmail_send import is_gmail_configured, send_platform_email
from entitlements import member_user_ids
from supabase_rest import email_from_user_id
from tiers import limits_for

WELCOME_SUBJECT = "Welcome to LogiQ - you're in."
WELCOME_FROM_NAME = "Hamza at LogiQ"
SUBSCRIPTION_SUBJECT = "Your LogiQ subscription is active - here's what to do next"

# FLAG — Welcome email timing:
# The ONLY live welcome path is POST /api/auth/welcome (after verifyOtp / email confirm).
# The Before User Created hook below no longer sends welcome mail (send commented out).
# Keep the commented block — once SMTP is fully set up we may move welcome permanently
# back to a confirmed-user hook; until then do not re-enable signup-time sends.


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
1. Connect your Gmail - takes 30 seconds and unlocks Aria and Nova immediately
2. Tell Blueprint AI what you want to automate - describe it in plain English and it builds the workflow
3. Review and approve your first workflow - nothing runs until you say so

If you get stuck or have any questions, reply to this email directly. I read every one.

Hamza
Founder, LogiQ
logiqops.co.uk

P.S. You're on early access pricing. That rate is locked in for as long as you stay a customer.
"""


def _first_name_from_email(email: str) -> str:
    addr = (email or "").strip()
    if addr and "@" in addr:
        return addr.split("@")[0]
    return "there"


def _subscription_body(first_name: str, tier_name: str, actions_limit: int) -> str:
    return f"""Hi {first_name},

Your {tier_name} subscription is now active. Welcome properly — you're live on LogiQ.

Here's what to do right now:

1. Connect your Gmail — go to Integrations in your dashboard and connect your Google account. Takes 30 seconds and unlocks Aria and Nova immediately.

2. Tell Blueprint AI what you want to automate — go to Build, describe your workflow in plain English, and LogiQ will build it for you.

3. Approve your first workflow — nothing runs until you say so. You're always in control.

Your plan: {tier_name} — {actions_limit} actions per month.

If you get stuck, reply to this email directly. I read every one.

Hamza
Founder, LogiQ
logiqops.co.uk

P.S. You're on founding client pricing. That rate is locked in as long as you stay subscribed.
"""


def send_welcome_email(email: str, first_name: str = "") -> Tuple[bool, str]:
    """
    Send the platform welcome email (post-OTP verification path).
    Called from POST /api/auth/welcome after successful verifyOtp — not from the signup hook long-term.
    """
    to = (email or "").strip()
    if not to:
        return False, "email required"
    if not is_gmail_configured():
        print("[welcome] Gmail not configured — skipping welcome email")
        return False, "gmail_not_configured"
    name = (first_name or "").strip() or _first_name_from_email(to)
    try:
        ok, detail = send_platform_email(
            to=to,
            subject=WELCOME_SUBJECT,
            body=_welcome_body(name),
            from_name=WELCOME_FROM_NAME,
        )
        if ok:
            print(f"[welcome] Sent post-verify welcome to {to} (id={detail})")
        else:
            print(f"[welcome] Failed for {to}: {detail}")
        return ok, str(detail)
    except Exception as exc:
        print(f"[welcome] Error for {to}: {exc}")
        traceback.print_exc()
        return False, str(exc)


def send_subscription_confirmation(client_id: str, tier: str) -> None:
    cid = (client_id or "").strip()
    tier_slug = (tier or "").strip().lower()
    if not cid or not tier_slug:
        print(f"[subscription_email] Missing client_id or tier — skipping")
        return

    if not is_gmail_configured():
        print("[subscription_email] Gmail not configured — skipping confirmation email")
        return

    tier_name = tier_slug.title()
    actions_limit = int(limits_for(tier_slug).get("actions") or 0)
    user_ids = member_user_ids(cid)
    if not user_ids:
        print(f"[subscription_email] No members for client {cid} — skipping")
        return

    for user_id in user_ids:
        email = email_from_user_id(user_id)
        if not email:
            print(f"[subscription_email] No email for user {user_id} — skipping")
            continue
        first_name = _first_name_from_email(email)
        try:
            ok, detail = send_platform_email(
                to=email,
                subject=SUBSCRIPTION_SUBJECT,
                body=_subscription_body(first_name, tier_name, actions_limit),
                from_name=WELCOME_FROM_NAME,
            )
            if ok:
                print(f"[subscription_email] Sent to {email} (id={detail})")
            else:
                print(f"[subscription_email] Failed for {email}: {detail}")
        except Exception as exc:
            print(f"[subscription_email] Error for {email}: {exc}")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    handler.send_response(status)
    handler.send_header("Content-type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(payload).encode())


def is_user_created_hook_path(path: str) -> bool:
    normalized = (path or "").rstrip("/").lower()
    return normalized.endswith("/hook/user-created") or normalized.endswith("/hooks/user-created")


def handle_user_created_hook(handler: BaseHTTPRequestHandler) -> None:
    from standardwebhooks.webhooks import Webhook, WebhookVerificationError

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
        print("[auth_hook] No email in hook payload — acknowledging")
        json_response(handler, 200, {})
        return

    if hook_name and hook_name not in ("before-user-created", "on_auth_user_created"):
        print(f"[auth_hook] Unhandled hook type: {hook_name}")

    # Welcome email DISABLED at signup (pre-OTP). ONLY path: POST /api/auth/welcome after verify.
    # FLAG: Leave this block commented — may restore here permanently once SMTP + confirmed-user
    # hook timing is fully set up. Do not re-enable until then (causes double welcome with OTP).
    # if not is_gmail_configured():
    #     print("[auth_hook] Gmail not configured — signup proceeds without welcome email")
    #     json_response(handler, 200, {})
    #     return
    # first_name = _first_name(user)
    # try:
    #     ok, detail = send_platform_email(
    #         to=email,
    #         subject=WELCOME_SUBJECT,
    #         body=_welcome_body(first_name),
    #         from_name=WELCOME_FROM_NAME,
    #     )
    #     if ok:
    #         print(f"[auth_hook] Welcome email sent to {email} (id={detail})")
    #     else:
    #         print(f"[auth_hook] Welcome email failed for {email}: {detail}")
    # except Exception as exc:
    #     print(f"[auth_hook] Welcome email error for {email}: {exc}")
    #     traceback.print_exc()
    print(f"[auth_hook] Welcome send skipped at signup for {email} — use POST /api/auth/welcome after OTP")

    json_response(handler, 200, {})
