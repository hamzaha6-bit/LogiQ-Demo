-- 001_rls_foundation.sql
-- Multi-tenant RLS foundation: clients + client_members, client_id backfill, policies.
-- Bootstrap: one client per existing auth.users row (1:1), role = owner.
-- clients.id is always a fresh UUID — mapping lives only in client_members.
-- RLS: client_id IN (SELECT client_id FROM client_members WHERE user_id = auth.uid())
--
-- Run in Supabase SQL Editor or via psql against the project database.
-- Requires: existing tables from backend/schema.sql

-- ─── Orphan holding client UUID collision guard ───────────────────────────────
DO $$
DECLARE
  orphan_uuid constant uuid := 'a0000000-0000-4000-8000-000000000099';
  tbl text;
  business_tables text[] := ARRAY[
    'user_integrations',
    'audit_log',
    'usage_tracking',
    'leads',
    'lead_emails',
    'invoices',
    'invoice_emails',
    'enquiries',
    'reports',
    'tasks',
    'agent_actions',
    'sheet_connections',
    'workflows',
    'workflow_approvals'
  ];
  collision_count int;
BEGIN
  IF EXISTS (SELECT 1 FROM auth.users WHERE id = orphan_uuid) THEN
    RAISE EXCEPTION
      'Orphan holding client UUID % already exists as auth.users.id — choose a different UUID',
      orphan_uuid;
  END IF;

  FOREACH tbl IN ARRAY business_tables
  LOOP
    EXECUTE format(
      'SELECT count(*)::int FROM %I WHERE user_id = $1',
      tbl
    )
    INTO collision_count
    USING orphan_uuid;

    IF collision_count > 0 THEN
      RAISE EXCEPTION
        'Orphan holding client UUID % already exists as user_id in % (% rows) — choose a different UUID',
        orphan_uuid, tbl, collision_count;
    END IF;
  END LOOP;
END $$;

BEGIN;

-- ─── Orphan holding client (no members — service-role access only) ─────────────
-- Rows with user_id IS NULL are flagged in migration_orphans and assigned here
-- so client_id NOT NULL can be enforced without deleting data.

CREATE TABLE IF NOT EXISTS clients (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL DEFAULT 'Workspace',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS client_members (
  client_id uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role text NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_client_members_user ON client_members(user_id);

CREATE TABLE IF NOT EXISTS migration_orphans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_table text NOT NULL,
  source_row_id uuid NOT NULL,
  reason text NOT NULL,
  flagged_at timestamptz NOT NULL DEFAULT now(),
  row_snapshot jsonb
);

CREATE INDEX IF NOT EXISTS idx_migration_orphans_source
  ON migration_orphans(source_table, source_row_id);

INSERT INTO clients (id, name)
VALUES ('a0000000-0000-4000-8000-000000000099', 'MIGRATION_UNASSIGNED_ORPHANS')
ON CONFLICT (id) DO NOTHING;

-- Bootstrap: one fresh client per auth user; mapping only in client_members.
DO $bootstrap$
DECLARE
  u RECORD;
  new_client_id uuid;
  client_name text;
BEGIN
  FOR u IN SELECT id, email, raw_user_meta_data FROM auth.users
  LOOP
    IF EXISTS (SELECT 1 FROM client_members cm WHERE cm.user_id = u.id) THEN
      CONTINUE;
    END IF;

    client_name := COALESCE(
      NULLIF(TRIM(u.raw_user_meta_data->>'name'), ''),
      NULLIF(TRIM(split_part(u.email, '@', 1)), ''),
      'Workspace'
    );

    INSERT INTO clients (name)
    VALUES (client_name)
    RETURNING id INTO new_client_id;

    INSERT INTO client_members (client_id, user_id, role)
    VALUES (new_client_id, u.id, 'owner');
  END LOOP;
END
$bootstrap$;

-- ─── Helper: resolve visible client_ids for the current JWT user ───────────────
-- SECURITY DEFINER avoids recursive RLS on client_members during policy checks.

CREATE OR REPLACE FUNCTION public.user_client_ids()
RETURNS SETOF uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT client_id FROM public.client_members WHERE user_id = auth.uid();
$$;

REVOKE ALL ON FUNCTION public.user_client_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.user_client_ids() TO authenticated;
GRANT EXECUTE ON FUNCTION public.user_client_ids() TO anon;

-- ─── Per business table ───────────────────────────────────────────────────────
-- 1) ADD client_id (nullable)
-- 2) FLAG orphans (user_id IS NULL) → migration_orphans
-- 3) BACKFILL client_id via client_members (owner role)
-- 4) ASSIGN orphan holding client for unmapped rows
-- 5) SET NOT NULL + index

-- user_integrations
ALTER TABLE user_integrations ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'user_integrations', ui.id, 'user_id IS NULL', to_jsonb(ui)
FROM user_integrations ui
WHERE ui.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'user_integrations' AND mo.source_row_id = ui.id
  );
UPDATE user_integrations ui
SET client_id = cm.client_id
FROM client_members cm
WHERE ui.user_id = cm.user_id
  AND ui.client_id IS NULL
  AND cm.role = 'owner';
UPDATE user_integrations
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE user_integrations ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_user_integrations_client ON user_integrations(client_id);

-- audit_log
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'audit_log', al.id, 'user_id IS NULL', to_jsonb(al)
FROM audit_log al
WHERE al.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'audit_log' AND mo.source_row_id = al.id
  );
UPDATE audit_log al
SET client_id = cm.client_id
FROM client_members cm
WHERE al.user_id = cm.user_id
  AND al.client_id IS NULL
  AND cm.role = 'owner';
UPDATE audit_log
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE audit_log ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_log_client ON audit_log(client_id);

-- usage_tracking
ALTER TABLE usage_tracking ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'usage_tracking', ut.id, 'user_id IS NULL', to_jsonb(ut)
FROM usage_tracking ut
WHERE ut.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'usage_tracking' AND mo.source_row_id = ut.id
  );
UPDATE usage_tracking ut
SET client_id = cm.client_id
FROM client_members cm
WHERE ut.user_id = cm.user_id
  AND ut.client_id IS NULL
  AND cm.role = 'owner';
UPDATE usage_tracking
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE usage_tracking ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_usage_tracking_client ON usage_tracking(client_id);

-- leads
ALTER TABLE leads ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'leads', l.id, 'user_id IS NULL', to_jsonb(l)
FROM leads l
WHERE l.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'leads' AND mo.source_row_id = l.id
  );
UPDATE leads l
SET client_id = cm.client_id
FROM client_members cm
WHERE l.user_id = cm.user_id
  AND l.client_id IS NULL
  AND cm.role = 'owner';
UPDATE leads
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE leads ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_client ON leads(client_id);

-- lead_emails
ALTER TABLE lead_emails ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'lead_emails', le.id, 'user_id IS NULL', to_jsonb(le)
FROM lead_emails le
WHERE le.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'lead_emails' AND mo.source_row_id = le.id
  );
UPDATE lead_emails le
SET client_id = cm.client_id
FROM client_members cm
WHERE le.user_id = cm.user_id
  AND le.client_id IS NULL
  AND cm.role = 'owner';
UPDATE lead_emails
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE lead_emails ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_lead_emails_client ON lead_emails(client_id);

-- invoices
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'invoices', i.id, 'user_id IS NULL', to_jsonb(i)
FROM invoices i
WHERE i.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'invoices' AND mo.source_row_id = i.id
  );
UPDATE invoices i
SET client_id = cm.client_id
FROM client_members cm
WHERE i.user_id = cm.user_id
  AND i.client_id IS NULL
  AND cm.role = 'owner';
UPDATE invoices
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE invoices ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_invoices_client ON invoices(client_id);

-- invoice_emails
ALTER TABLE invoice_emails ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'invoice_emails', ie.id, 'user_id IS NULL', to_jsonb(ie)
FROM invoice_emails ie
WHERE ie.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'invoice_emails' AND mo.source_row_id = ie.id
  );
UPDATE invoice_emails ie
SET client_id = cm.client_id
FROM client_members cm
WHERE ie.user_id = cm.user_id
  AND ie.client_id IS NULL
  AND cm.role = 'owner';
UPDATE invoice_emails
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE invoice_emails ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_invoice_emails_client ON invoice_emails(client_id);

-- enquiries
ALTER TABLE enquiries ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'enquiries', e.id, 'user_id IS NULL', to_jsonb(e)
FROM enquiries e
WHERE e.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'enquiries' AND mo.source_row_id = e.id
  );
UPDATE enquiries e
SET client_id = cm.client_id
FROM client_members cm
WHERE e.user_id = cm.user_id
  AND e.client_id IS NULL
  AND cm.role = 'owner';
UPDATE enquiries
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE enquiries ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_enquiries_client ON enquiries(client_id);

-- reports
ALTER TABLE reports ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'reports', r.id, 'user_id IS NULL', to_jsonb(r)
FROM reports r
WHERE r.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'reports' AND mo.source_row_id = r.id
  );
UPDATE reports r
SET client_id = cm.client_id
FROM client_members cm
WHERE r.user_id = cm.user_id
  AND r.client_id IS NULL
  AND cm.role = 'owner';
UPDATE reports
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE reports ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_reports_client ON reports(client_id);

-- tasks
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'tasks', t.id, 'user_id IS NULL', to_jsonb(t)
FROM tasks t
WHERE t.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'tasks' AND mo.source_row_id = t.id
  );
UPDATE tasks t
SET client_id = cm.client_id
FROM client_members cm
WHERE t.user_id = cm.user_id
  AND t.client_id IS NULL
  AND cm.role = 'owner';
UPDATE tasks
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE tasks ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_client ON tasks(client_id);

-- agent_actions
ALTER TABLE agent_actions ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'agent_actions', aa.id, 'user_id IS NULL', to_jsonb(aa)
FROM agent_actions aa
WHERE aa.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'agent_actions' AND mo.source_row_id = aa.id
  );
UPDATE agent_actions aa
SET client_id = cm.client_id
FROM client_members cm
WHERE aa.user_id = cm.user_id
  AND aa.client_id IS NULL
  AND cm.role = 'owner';
UPDATE agent_actions
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE agent_actions ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_actions_client ON agent_actions(client_id);

-- sheet_connections
ALTER TABLE sheet_connections ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'sheet_connections', sc.id, 'user_id IS NULL', to_jsonb(sc)
FROM sheet_connections sc
WHERE sc.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'sheet_connections' AND mo.source_row_id = sc.id
  );
UPDATE sheet_connections sc
SET client_id = cm.client_id
FROM client_members cm
WHERE sc.user_id = cm.user_id
  AND sc.client_id IS NULL
  AND cm.role = 'owner';
UPDATE sheet_connections
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE sheet_connections ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sheet_connections_client ON sheet_connections(client_id);

-- workflows
ALTER TABLE workflows ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'workflows', w.id, 'user_id IS NULL', to_jsonb(w)
FROM workflows w
WHERE w.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'workflows' AND mo.source_row_id = w.id
  );
UPDATE workflows w
SET client_id = cm.client_id
FROM client_members cm
WHERE w.user_id = cm.user_id
  AND w.client_id IS NULL
  AND cm.role = 'owner';
UPDATE workflows
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE workflows ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workflows_client ON workflows(client_id);

-- workflow_approvals
ALTER TABLE workflow_approvals ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id);
INSERT INTO migration_orphans (source_table, source_row_id, reason, row_snapshot)
SELECT 'workflow_approvals', wa.id, 'user_id IS NULL', to_jsonb(wa)
FROM workflow_approvals wa
WHERE wa.user_id IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM migration_orphans mo
    WHERE mo.source_table = 'workflow_approvals' AND mo.source_row_id = wa.id
  );
UPDATE workflow_approvals wa
SET client_id = cm.client_id
FROM client_members cm
WHERE wa.user_id = cm.user_id
  AND wa.client_id IS NULL
  AND cm.role = 'owner';
UPDATE workflow_approvals
SET client_id = 'a0000000-0000-4000-8000-000000000099'
WHERE client_id IS NULL;
ALTER TABLE workflow_approvals ALTER COLUMN client_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_approvals_client ON workflow_approvals(client_id);

-- ─── Enable RLS ───────────────────────────────────────────────────────────────

ALTER TABLE clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE migration_orphans ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_integrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_tracking ENABLE ROW LEVEL SECURITY;
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead_emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoice_emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE enquiries ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sheet_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflows ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_approvals ENABLE ROW LEVEL SECURITY;

-- ─── clients + client_members policies ───────────────────────────────────────

CREATE POLICY clients_member_select ON clients
  FOR SELECT TO authenticated
  USING (id IN (SELECT public.user_client_ids()));

CREATE POLICY clients_service_role_all ON clients
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY client_members_member_select ON client_members
  FOR SELECT TO authenticated
  USING (client_id IN (SELECT public.user_client_ids()));

CREATE POLICY client_members_service_role_all ON client_members
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY migration_orphans_service_role_all ON migration_orphans
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- user_profiles: user-scoped (no client_id)
CREATE POLICY user_profiles_own_select ON user_profiles
  FOR SELECT TO authenticated
  USING (id = auth.uid());

CREATE POLICY user_profiles_own_insert ON user_profiles
  FOR INSERT TO authenticated
  WITH CHECK (id = auth.uid());

CREATE POLICY user_profiles_own_update ON user_profiles
  FOR UPDATE TO authenticated
  USING (id = auth.uid())
  WITH CHECK (id = auth.uid());

CREATE POLICY user_profiles_service_role_all ON user_profiles
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- ─── Business table policies (membership join) ────────────────────────────────
-- AUDIT FLAG: sheet_connections updates in api_lib/sheets_service.py PATCH by id
-- only without client_id in the filter — address in a dedicated future PR.

DO $policy$
DECLARE
  tbl text;
  tables text[] := ARRAY[
    'user_integrations',
    'audit_log',
    'usage_tracking',
    'leads',
    'lead_emails',
    'invoices',
    'invoice_emails',
    'enquiries',
    'reports',
    'tasks',
    'agent_actions',
    'sheet_connections',
    'workflows',
    'workflow_approvals'
  ];
BEGIN
  FOREACH tbl IN ARRAY tables
  LOOP
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR SELECT TO authenticated USING (client_id IN (SELECT public.user_client_ids()))',
      tbl || '_client_select', tbl
    );
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR INSERT TO authenticated WITH CHECK (client_id IN (SELECT public.user_client_ids()))',
      tbl || '_client_insert', tbl
    );
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR UPDATE TO authenticated USING (client_id IN (SELECT public.user_client_ids())) WITH CHECK (client_id IN (SELECT public.user_client_ids()))',
      tbl || '_client_update', tbl
    );
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR DELETE TO authenticated USING (client_id IN (SELECT public.user_client_ids()))',
      tbl || '_client_delete', tbl
    );
    EXECUTE format(
      'CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
      tbl || '_service_role_all', tbl
    );
  END LOOP;
END
$policy$;

COMMIT;

-- ============================================================================
-- FUTURE WORK (not in this migration — dedicated PRs)
-- ============================================================================
--
-- AUDIT FLAGS (section 6 — do not fix here):
--   • index.html: dbGet(table, {}) unfiltered reads; dbInsert without user_id;
--     dbUpdate by id only; supabaseClient never receives user JWT session.
--   • api_lib/sheets_service.py + backend/sheets_service.py: sheet_connections
--     PATCH by connection id only — no client_id in the REST filter.
--   • api_lib/supabase_rest.py + backend/supabase_client.py: all REST calls use
--     SUPABASE_SERVICE_KEY and bypass RLS until migrated to user JWT reads.
--   • Child tables lack FK enforcement — cross-tenant risk via guessed parent ids.
--
-- SUGGESTED CHILD-TABLE FKs (comment only — do not apply in this PR):
--   ALTER TABLE lead_emails
--     ADD CONSTRAINT lead_emails_lead_id_fkey
--     FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE;
--   ALTER TABLE invoice_emails
--     ADD CONSTRAINT invoice_emails_invoice_id_fkey
--     FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE;
--   ALTER TABLE workflow_approvals
--     ADD CONSTRAINT workflow_approvals_workflow_id_fkey
--     FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE;
--
-- ============================================================================
-- DOWN — run manually to reverse 001_rls_foundation
-- ============================================================================
/*
BEGIN;

DO $down$
DECLARE
  tbl text;
  tables text[] := ARRAY[
    'user_integrations', 'audit_log', 'usage_tracking', 'leads', 'lead_emails',
    'invoices', 'invoice_emails', 'enquiries', 'reports', 'tasks', 'agent_actions',
    'sheet_connections', 'workflows', 'workflow_approvals'
  ];
BEGIN
  FOREACH tbl IN ARRAY tables
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', tbl || '_client_select', tbl);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', tbl || '_client_insert', tbl);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', tbl || '_client_update', tbl);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', tbl || '_client_delete', tbl);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', tbl || '_service_role_all', tbl);
    EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', tbl);
    EXECUTE format('DROP INDEX IF EXISTS %I', 'idx_' || tbl || '_client');
    EXECUTE format('ALTER TABLE %I DROP COLUMN IF EXISTS client_id', tbl);
  END LOOP;
END
$down$;

DROP POLICY IF EXISTS user_profiles_own_select ON user_profiles;
DROP POLICY IF EXISTS user_profiles_own_insert ON user_profiles;
DROP POLICY IF EXISTS user_profiles_own_update ON user_profiles;
DROP POLICY IF EXISTS user_profiles_service_role_all ON user_profiles;
ALTER TABLE user_profiles DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS clients_member_select ON clients;
DROP POLICY IF EXISTS clients_service_role_all ON clients;
ALTER TABLE clients DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS client_members_member_select ON client_members;
DROP POLICY IF EXISTS client_members_service_role_all ON client_members;
ALTER TABLE client_members DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS migration_orphans_service_role_all ON migration_orphans;
ALTER TABLE migration_orphans DISABLE ROW LEVEL SECURITY;

DROP FUNCTION IF EXISTS public.user_client_ids();

DROP TABLE IF EXISTS migration_orphans;
DROP TABLE IF EXISTS client_members;
DELETE FROM clients WHERE id = 'a0000000-0000-4000-8000-000000000099';
-- Bootstrapped clients (fresh UUIDs) are removed when client_members is dropped
-- if you also delete from clients: DELETE FROM clients WHERE id NOT IN (SELECT id FROM auth.users);

COMMIT;
*/
