-- 003_client_usage.sql
-- Client-scoped monthly action and spend tracking for the execution gate (step 5).
-- Requires: 001_rls_foundation.sql (clients, user_client_ids()).

BEGIN;

CREATE TABLE IF NOT EXISTS client_usage (
  client_id uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  month date NOT NULL,
  actions_used int NOT NULL DEFAULT 0,
  spend_pence int NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, month)
);

CREATE INDEX IF NOT EXISTS idx_client_usage_month ON client_usage(month);

ALTER TABLE client_usage ENABLE ROW LEVEL SECURITY;

CREATE POLICY client_usage_member_select ON client_usage
  FOR SELECT TO authenticated
  USING (client_id IN (SELECT public.user_client_ids()));

CREATE POLICY client_usage_service_role_all ON client_usage
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

COMMIT;

-- ─── DOWN (run manually to reverse) ──────────────────────────────────────────
/*
BEGIN;
DROP POLICY IF EXISTS client_usage_member_select ON client_usage;
DROP POLICY IF EXISTS client_usage_service_role_all ON client_usage;
ALTER TABLE client_usage DISABLE ROW LEVEL SECURITY;
DROP INDEX IF EXISTS idx_client_usage_month;
DROP TABLE IF EXISTS client_usage;
COMMIT;
*/
