-- 012_workflow_isolation.sql
-- RLS + user ownership on workflow_runs; user-scoped policies on workflows /
-- workflow_approvals; one active run per workflow.
-- FLAG: Apply manually in Supabase SQL Editor (after 011_sheet_source_tab.sql).

BEGIN;

ALTER TABLE workflow_runs
  ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE;

ALTER TABLE workflow_runs
  ADD COLUMN IF NOT EXISTS client_id uuid REFERENCES clients(id) ON DELETE CASCADE;

UPDATE workflow_runs wr
SET user_id = w.user_id
FROM workflows w
WHERE wr.workflow_id = w.id
  AND wr.user_id IS NULL;

UPDATE workflow_runs wr
SET client_id = w.client_id
FROM workflows w
WHERE wr.workflow_id = w.id
  AND wr.client_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_runs_user ON workflow_runs(user_id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_client ON workflow_runs(client_id);

-- Collapse duplicate in-flight runs before the uniqueness lock.
WITH ranked AS (
  SELECT id,
         ROW_NUMBER() OVER (PARTITION BY workflow_id ORDER BY started_at DESC NULLS LAST) AS rn
  FROM workflow_runs
  WHERE status IN ('running', 'paused')
    AND workflow_id IS NOT NULL
)
UPDATE workflow_runs
SET status = 'cancelled',
    error = 'Superseded by concurrent-run lock (012)',
    completed_at = COALESCE(completed_at, now())
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- E16: at most one running/paused run per workflow (cron + manual + multi-instance).
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_one_active
  ON workflow_runs (workflow_id)
  WHERE status IN ('running', 'paused') AND workflow_id IS NOT NULL;

ALTER TABLE workflow_runs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS workflow_runs_own_select ON workflow_runs;
DROP POLICY IF EXISTS workflow_runs_own_insert ON workflow_runs;
DROP POLICY IF EXISTS workflow_runs_own_update ON workflow_runs;
DROP POLICY IF EXISTS workflow_runs_own_delete ON workflow_runs;
DROP POLICY IF EXISTS workflow_runs_service_role_all ON workflow_runs;

CREATE POLICY workflow_runs_own_select ON workflow_runs
  FOR SELECT TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY workflow_runs_own_insert ON workflow_runs
  FOR INSERT TO authenticated
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY workflow_runs_own_update ON workflow_runs
  FOR UPDATE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  )
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY workflow_runs_own_delete ON workflow_runs
  FOR DELETE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY workflow_runs_service_role_all ON workflow_runs
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- G10: client membership is not enough — workflows/approvals are per-user.
DROP POLICY IF EXISTS workflows_client_select ON workflows;
DROP POLICY IF EXISTS workflows_client_insert ON workflows;
DROP POLICY IF EXISTS workflows_client_update ON workflows;
DROP POLICY IF EXISTS workflows_client_delete ON workflows;

CREATE POLICY workflows_own_select ON workflows
  FOR SELECT TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY workflows_own_insert ON workflows
  FOR INSERT TO authenticated
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY workflows_own_update ON workflows
  FOR UPDATE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  )
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY workflows_own_delete ON workflows
  FOR DELETE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

DROP POLICY IF EXISTS workflow_approvals_client_select ON workflow_approvals;
DROP POLICY IF EXISTS workflow_approvals_client_insert ON workflow_approvals;
DROP POLICY IF EXISTS workflow_approvals_client_update ON workflow_approvals;
DROP POLICY IF EXISTS workflow_approvals_client_delete ON workflow_approvals;

CREATE POLICY workflow_approvals_own_select ON workflow_approvals
  FOR SELECT TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY workflow_approvals_own_insert ON workflow_approvals
  FOR INSERT TO authenticated
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY workflow_approvals_own_update ON workflow_approvals
  FOR UPDATE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  )
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY workflow_approvals_own_delete ON workflow_approvals
  FOR DELETE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

COMMIT;
