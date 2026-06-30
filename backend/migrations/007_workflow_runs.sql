-- 007_workflow_runs.sql
-- Persist workflow execution context for auditable, resumable runs.
-- APPLY MANUALLY in Supabase SQL Editor — committing this file does NOT apply it.
--
-- workflow_runs.context_json shape:
--   { "step_1": { "output": { ... } }, "step_2": { "output": { ... } } }

CREATE TABLE IF NOT EXISTS workflow_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id uuid REFERENCES workflows(id) ON DELETE CASCADE,
  started_at timestamptz DEFAULT now(),
  completed_at timestamptz,
  status text DEFAULT 'running',
  context_json jsonb DEFAULT '{}'::jsonb,
  error text
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow
  ON workflow_runs (workflow_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_status
  ON workflow_runs (status)
  WHERE status IN ('running', 'paused');

ALTER TABLE workflow_approvals
  ADD COLUMN IF NOT EXISTS workflow_run_id uuid REFERENCES workflow_runs(id) ON DELETE SET NULL;
