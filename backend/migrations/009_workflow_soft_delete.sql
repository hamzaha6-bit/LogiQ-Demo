-- 009_workflow_soft_delete.sql
-- Soft-delete support for Blueprint workflows.
-- FLAG: Apply manually in Supabase SQL Editor (after 008_agent_limits.sql).

BEGIN;

ALTER TABLE workflows
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz DEFAULT NULL;

-- Allow status='deleted' alongside existing values (active / paused / etc.).
-- No CHECK constraint on status historically — document convention only:
--   active | paused | deleted

CREATE INDEX IF NOT EXISTS idx_workflows_user_not_deleted
  ON workflows (user_id, created_at DESC)
  WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_workflows_active_due
  ON workflows (next_run_at)
  WHERE status = 'active' AND deleted_at IS NULL AND schedule IS NOT NULL;

COMMIT;

-- ─── DOWN (run manually to reverse) ──────────────────────────────────────────
/*
BEGIN;

DROP INDEX IF EXISTS idx_workflows_active_due;
DROP INDEX IF EXISTS idx_workflows_user_not_deleted;
ALTER TABLE workflows DROP COLUMN IF EXISTS deleted_at;

COMMIT;
*/
