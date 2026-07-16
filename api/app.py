"""Core app routes: ping, health, config, billing, audit, workflow controls."""
from http.server import BaseHTTPRequestHandler
import json
import os
import re
import sys
from urllib.parse import urlparse

_API_DIR = os.path.dirname(os.path.abspath(__file__))
_API_LIB = os.path.normpath(os.path.join(_API_DIR, "..", "api_lib"))
if _API_LIB not in sys.path:
    sys.path.insert(0, _API_LIB)

from admin_dashboard import build_admin_dashboard
from billing_checkout import CheckoutError, process_checkout
from billing_portal import PortalError, process_portal
from billing_status import billing_status_for_request
from billing_webhook import WebhookError, process_event
from client_agents import activate_agent_for_user, agents_status_for_user, pause_agent_for_user
from http_auth import resolve_user_id
from supabase_rest import pause_workflows_for_user
from topup_checkout import TopupError, process_topup
from workflow_create import create_workflow_for_user
from workflow_delete import soft_delete_workflow_for_user
from workflow_queries import latest_run_for_user_workflow, list_workflows_for_user
from workflow_runner import run_due_scheduled_workflows, run_workflow_for_user


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")

        latest_run_match = re.search(r"/workflows/([^/]+)/runs/latest$", path)

        if path.endswith("/ping"):
            self._json(200, {"status": "ok", "version": "2"})
        elif path.endswith("/health"):
            self._json(
                200,
                {
                    "status": "ok",
                    "anthropic_configured": bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
                    "supabase_configured": bool(os.environ.get("SUPABASE_URL")),
                },
            )
        elif path.endswith("/config"):
            self._json(
                200,
                {
                    "supabase_url": os.environ.get("SUPABASE_URL", ""),
                    "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
                    "supabase_configured": bool(os.environ.get("SUPABASE_URL")),
                    "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
                    "stripe_configured": bool((os.environ.get("STRIPE_SECRET_KEY") or "").strip()),
                },
            )
        elif path.endswith("/billing/status"):
            user_id = resolve_user_id(self)
            status, payload = billing_status_for_request(user_id)
            self._json(status, payload)
        elif path.endswith("/agents/status"):
            self._agents_status()
        elif path.endswith("/admin/dashboard"):
            self._admin_dashboard()
        elif latest_run_match:
            self._workflow_latest_run(latest_run_match.group(1))
        elif path.endswith("/workflows"):
            self._list_workflows()
        elif path.endswith("/audit/log"):
            self._json(200, {"logs": [], "entries": []})
        elif path.endswith("/cron/workflows"):
            self._cron_run_workflows()
        else:
            self._json(404, {"detail": f"Unknown route: {path}"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path.endswith("/workflows/emergency-stop"):
            self._emergency_stop_workflows()
        elif path.endswith("/workflows/create"):
            self._create_workflow()
        elif path.endswith("/workflows/run"):
            self._run_workflow()
        elif path.endswith("/workflows/delete"):
            self._delete_workflow()
        elif path.endswith("/agents/activate"):
            self._agents_activate()
        elif path.endswith("/agents/pause"):
            self._agents_pause()
        elif path.endswith("/billing/checkout"):
            self._billing_checkout()
        elif path.endswith("/billing/topup"):
            self._billing_topup()
        elif path.endswith("/billing/portal"):
            self._billing_portal()
        elif path.endswith("/billing/webhook"):
            self._billing_webhook()
        else:
            self._json(404, {"detail": f"Unknown route: {path}"})

    def _read_raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_json_body(self) -> dict:
        raw = self._read_raw_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _billing_checkout(self):
        user_id = resolve_user_id(self)
        body = self._read_json_body()
        try:
            result = process_checkout(user_id, body.get("tier"))
            self._json(200, result)
        except CheckoutError as exc:
            self._json(exc.status, {"detail": exc.detail})

    def _billing_topup(self):
        user_id = resolve_user_id(self)
        body = self._read_json_body()
        try:
            result = process_topup(user_id, body.get("pack_size"))
            self._json(200, result)
        except TopupError as exc:
            self._json(exc.status, {"detail": exc.detail})

    def _billing_portal(self):
        user_id = resolve_user_id(self)
        try:
            result = process_portal(user_id)
            self._json(200, result)
        except PortalError as exc:
            self._json(exc.status, exc.payload)

    def _billing_webhook(self):
        payload = self._read_raw_body()
        sig = self.headers.get("Stripe-Signature", "")
        try:
            result = process_event(payload, sig)
            self._json(200, result)
        except WebhookError as exc:
            self._json(exc.status, {"detail": exc.detail})

    def _emergency_stop_workflows(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return

        paused_count, err = pause_workflows_for_user(user_id, active_only=True)
        if err:
            self._json(502, {"detail": err})
            return

        self._json(200, {"status": "ok", "paused_count": paused_count})

    def _run_workflow(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return

        body = self._read_json_body()
        workflow_id = (body.get("workflow_id") or "").strip()
        if not workflow_id:
            self._json(400, {"detail": "workflow_id is required"})
            return

        workflow_run_id = (body.get("workflow_run_id") or "").strip() or None
        approval_id = (body.get("approval_id") or "").strip() or None

        status, payload = run_workflow_for_user(
            user_id,
            workflow_id,
            workflow_run_id=workflow_run_id,
            approval_id=approval_id,
        )
        self._json(status, payload)

    def _create_workflow(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return
        body = self._read_json_body()
        status, payload = create_workflow_for_user(user_id, body)
        self._json(status, payload)

    def _delete_workflow(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return
        body = self._read_json_body()
        workflow_id = (body.get("workflow_id") or "").strip()
        status, payload = soft_delete_workflow_for_user(user_id, workflow_id)
        self._json(status, payload)

    def _agents_status(self):
        user_id = resolve_user_id(self)
        status, payload = agents_status_for_user(user_id or "")
        self._json(status, payload)

    def _list_workflows(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return
        status, payload = list_workflows_for_user(user_id)
        self._json(status, payload)

    def _workflow_latest_run(self, workflow_id: str):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return
        status, payload = latest_run_for_user_workflow(user_id, workflow_id)
        self._json(status, payload)

    def _admin_dashboard(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return
        status, payload = build_admin_dashboard(user_id)
        self._json(status, payload)

    def _agents_activate(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return
        body = self._read_json_body()
        status, payload = activate_agent_for_user(user_id, body.get("agent_id") or "")
        self._json(status, payload)

    def _agents_pause(self):
        user_id = resolve_user_id(self)
        if not user_id:
            self._json(401, {"detail": "Valid Bearer token required"})
            return
        body = self._read_json_body()
        status, payload = pause_agent_for_user(user_id, body.get("agent_id") or "")
        self._json(status, payload)

    def _cron_run_workflows(self):
        secret = (os.environ.get("CRON_SECRET") or "").strip()
        if not secret:
            self._json(503, {"detail": "CRON_SECRET not configured"})
            return
        auth = (self.headers.get("Authorization") or "").strip()
        if auth != f"Bearer {secret}":
            self._json(401, {"detail": "Unauthorized"})
            return

        try:
            result = run_due_scheduled_workflows()
            self._json(200, result)
        except Exception as exc:
            print(f"[cron/workflows] failed: {exc}")
            self._json(500, {"detail": "Scheduled workflow run failed"})

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
