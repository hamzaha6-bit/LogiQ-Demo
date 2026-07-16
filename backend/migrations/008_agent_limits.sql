-- 008_agent_limits.sql
-- Client-scoped agent activation + onboarding profile fields for next PR.
-- Requires: 001_rls_foundation.sql (clients), 002_entitlements.sql.
--
-- FLAG: Apply manually in Supabase SQL Editor.
-- Also apply 009_workflow_soft_delete.sql for workflow soft-delete.

BEGIN;

-- ─── Onboarding fields (user_profiles) — used by next-PR onboarding UI ───────
ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS onboarding_vertical text DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS onboarding_pain_points text DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS onboarding_completed_at timestamptz DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS welcome_sent_at timestamptz DEFAULT NULL;

-- ─── Active agents per client (source of truth for tier agent limits) ───────
CREATE TABLE IF NOT EXISTS client_agents (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  client_id uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  agent_id text NOT NULL,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'paused')),
  activated_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (client_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_client_agents_client_status
  ON client_agents (client_id, status);

ALTER TABLE client_agents ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS client_agents_member_select ON client_agents;
CREATE POLICY client_agents_member_select ON client_agents
  FOR SELECT TO authenticated
  USING (client_id IN (SELECT public.user_client_ids()));

DROP POLICY IF EXISTS client_agents_service_role_all ON client_agents;
CREATE POLICY client_agents_service_role_all ON client_agents
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

COMMIT;

-- ─── DOWN (run manually to reverse) ──────────────────────────────────────────
/*
BEGIN;

DROP POLICY IF EXISTS client_agents_member_select ON client_agents;
DROP POLICY IF EXISTS client_agents_service_role_all ON client_agents;
DROP INDEX IF EXISTS idx_client_agents_client_status;
DROP TABLE IF EXISTS client_agents;

ALTER TABLE user_profiles
  DROP COLUMN IF EXISTS onboarding_vertical,
  DROP COLUMN IF EXISTS onboarding_pain_points,
  DROP COLUMN IF EXISTS onboarding_completed_at,
  DROP COLUMN IF EXISTS welcome_sent_at;

COMMIT;
*/
