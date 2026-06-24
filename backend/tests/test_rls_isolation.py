"""RLS isolation tests — requires migration 001 applied and Supabase env vars.

Run:
  cd backend && pytest tests/test_rls_isolation.py -v

Requires:
  SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY
"""

from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or ""
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""

pytestmark = pytest.mark.skipif(
    not (SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_SERVICE_KEY),
    reason="SUPABASE_URL, SUPABASE_ANON_KEY, and SUPABASE_SERVICE_KEY required",
)


def _service_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _user_headers(access_token: str) -> Dict[str, str]:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _rest_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def _auth_admin_url() -> str:
    return f"{SUPABASE_URL}/auth/v1/admin/users"


def _auth_token_url() -> str:
    return f"{SUPABASE_URL}/auth/v1/token?grant_type=password"


class SupabaseTestContext:
    """Two isolated clients, one user each, with sample rows in leads + workflows."""

    def __init__(self) -> None:
        self.suffix = secrets.token_hex(4)
        self.password = f"Test-{secrets.token_hex(12)}!"
        self.users: List[Dict[str, Any]] = []
        self.clients: List[str] = []
        self.lead_ids: List[str] = []
        self.workflow_ids: List[str] = []

    @property
    def user_a(self) -> Dict[str, Any]:
        return self.users[0]

    @property
    def user_b(self) -> Dict[str, Any]:
        return self.users[1]

    def setup(self) -> None:
        for label in ("a", "b"):
            email = f"rls-test-{label}-{self.suffix}@example.com"
            user_id = self._create_auth_user(email)
            client_id = self._ensure_client_and_membership(user_id, email)
            token = self._sign_in(email)
            self.users.append(
                {
                    "id": user_id,
                    "email": email,
                    "client_id": client_id,
                    "access_token": token,
                }
            )
            self.clients.append(client_id)

        for user in self.users:
            self._ensure_user_profile(user)

        self.lead_ids = [
            self._insert_row("leads", self.user_a, {"name": "Lead A", "status": "new"}),
            self._insert_row("leads", self.user_b, {"name": "Lead B", "status": "new"}),
        ]
        self.workflow_ids = [
            self._insert_row(
                "workflows",
                self.user_a,
                {
                    "agent_id": "aria",
                    "name": "Workflow A",
                    "steps": [],
                    "status": "active",
                },
            ),
            self._insert_row(
                "workflows",
                self.user_b,
                {
                    "agent_id": "nova",
                    "name": "Workflow B",
                    "steps": [],
                    "status": "active",
                },
            ),
        ]

    def teardown(self) -> None:
        with httpx.Client(timeout=30) as client:
            for table, ids in (
                ("workflows", self.workflow_ids),
                ("leads", self.lead_ids),
            ):
                for row_id in ids:
                    if row_id:
                        client.delete(
                            _rest_url(table),
                            headers=_service_headers(),
                            params={"id": f"eq.{row_id}"},
                        )

            for user in self.users:
                client.delete(
                    _rest_url("user_profiles"),
                    headers=_service_headers(),
                    params={"id": f"eq.{user['id']}"},
                )
                client.delete(
                    _rest_url("client_members"),
                    headers=_service_headers(),
                    params={"user_id": f"eq.{user['id']}"},
                )
                client.delete(
                    _rest_url("clients"),
                    headers=_service_headers(),
                    params={"id": f"eq.{user['client_id']}"},
                )
                client.delete(
                    f"{_auth_admin_url()}/{user['id']}",
                    headers=_service_headers(),
                )

    def _create_auth_user(self, email: str) -> str:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                _auth_admin_url(),
                headers=_service_headers(),
                json={
                    "email": email,
                    "password": self.password,
                    "email_confirm": True,
                },
            )
            resp.raise_for_status()
            return resp.json()["id"]

    def _ensure_client_and_membership(self, user_id: str, email: str) -> str:
        client_id = str(uuid.uuid4())
        name = email.split("@")[0]
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                _rest_url("clients"),
                headers=_service_headers(),
                json={"id": client_id, "name": name},
            )
            resp.raise_for_status()
            resp = client.post(
                _rest_url("client_members"),
                headers=_service_headers(),
                json={"client_id": client_id, "user_id": user_id, "role": "owner"},
            )
            resp.raise_for_status()
        return client_id

    def _ensure_user_profile(self, user: Dict[str, Any]) -> None:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                _rest_url("user_profiles"),
                headers=_service_headers(),
                json={
                    "id": user["id"],
                    "name": user["email"].split("@")[0],
                    "plan": "starter",
                    "onboarding_complete": False,
                },
            )
            if resp.status_code not in (200, 201, 409):
                resp.raise_for_status()

    def _sign_in(self, email: str) -> str:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                _auth_token_url(),
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Content-Type": "application/json",
                },
                json={"email": email, "password": self.password},
            )
            resp.raise_for_status()
            return resp.json()["access_token"]

    def _insert_row(self, table: str, user: Dict[str, Any], payload: Dict[str, Any]) -> str:
        row = {
            **payload,
            "user_id": user["id"],
            "client_id": user["client_id"],
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(_rest_url(table), headers=_service_headers(), json=row)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data[0]["id"]
            return data["id"]

    def select_as_user(self, table: str, access_token: str, params: Optional[Dict[str, str]] = None) -> httpx.Response:
        with httpx.Client(timeout=30) as client:
            return client.get(
                _rest_url(table),
                headers=_user_headers(access_token),
                params=params or {"select": "id"},
            )

    def select_row_by_id_as_user(self, table: str, access_token: str, row_id: str) -> httpx.Response:
        select_cols = "id,name,client_id" if table != "user_profiles" else "id,name,plan"
        return self.select_as_user(
            table,
            access_token,
            {"id": f"eq.{row_id}", "select": select_cols},
        )

    def select_as_service(self, table: str, params: Optional[Dict[str, str]] = None) -> httpx.Response:
        with httpx.Client(timeout=30) as client:
            return client.get(
                _rest_url(table),
                headers=_service_headers(),
                params=params or {"select": "id"},
            )


@pytest.fixture(scope="module")
def rls_ctx() -> SupabaseTestContext:
    ctx = SupabaseTestContext()
    ctx.setup()
    yield ctx
    ctx.teardown()


class TestRlsIsolation:
    """Verify RLS allows same-client reads and blocks cross-client reads."""

    def test_user_a_reads_own_leads_not_client_b(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_as_user("leads", rls_ctx.user_a["access_token"], {"select": "id,name"})
        assert resp.status_code == 200
        rows = resp.json()
        ids = {row["id"] for row in rows}
        assert rls_ctx.lead_ids[0] in ids
        assert rls_ctx.lead_ids[1] not in ids

    def test_user_b_reads_own_leads_not_client_a(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_as_user("leads", rls_ctx.user_b["access_token"], {"select": "id,name"})
        assert resp.status_code == 200
        rows = resp.json()
        ids = {row["id"] for row in rows}
        assert rls_ctx.lead_ids[1] in ids
        assert rls_ctx.lead_ids[0] not in ids

    def test_user_a_cannot_read_client_b_lead_by_id(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_row_by_id_as_user(
            "leads", rls_ctx.user_a["access_token"], rls_ctx.lead_ids[1]
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_user_a_can_read_own_lead_by_id(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_row_by_id_as_user(
            "leads", rls_ctx.user_a["access_token"], rls_ctx.lead_ids[0]
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["id"] == rls_ctx.lead_ids[0]

    def test_user_a_reads_own_workflows_not_client_b(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_as_user("workflows", rls_ctx.user_a["access_token"], {"select": "id,name"})
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()}
        assert rls_ctx.workflow_ids[0] in ids
        assert rls_ctx.workflow_ids[1] not in ids

    def test_user_b_can_read_own_workflow_by_id(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_row_by_id_as_user(
            "workflows", rls_ctx.user_b["access_token"], rls_ctx.workflow_ids[1]
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["id"] == rls_ctx.workflow_ids[1]

    def test_user_b_cannot_read_client_a_workflow_by_id(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_row_by_id_as_user(
            "workflows", rls_ctx.user_b["access_token"], rls_ctx.workflow_ids[0]
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_user_sees_only_own_client_membership(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_as_user(
            "client_members",
            rls_ctx.user_a["access_token"],
            {"select": "client_id,user_id,role"},
        )
        assert resp.status_code == 200
        rows = resp.json()
        client_ids = {row["client_id"] for row in rows}
        assert rls_ctx.user_a["client_id"] in client_ids
        assert rls_ctx.user_b["client_id"] not in client_ids

    def test_user_a_reads_own_user_profile(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_row_by_id_as_user(
            "user_profiles", rls_ctx.user_a["access_token"], rls_ctx.user_a["id"]
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["id"] == rls_ctx.user_a["id"]

    def test_user_a_cannot_read_user_b_profile(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_row_by_id_as_user(
            "user_profiles", rls_ctx.user_a["access_token"], rls_ctx.user_b["id"]
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_service_role_reads_both_user_profiles(self, rls_ctx: SupabaseTestContext) -> None:
        resp = rls_ctx.select_as_service(
            "user_profiles",
            {
                "id": f"in.({rls_ctx.user_a['id']},{rls_ctx.user_b['id']})",
                "select": "id",
            },
        )
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()}
        assert rls_ctx.user_a["id"] in ids
        assert rls_ctx.user_b["id"] in ids
