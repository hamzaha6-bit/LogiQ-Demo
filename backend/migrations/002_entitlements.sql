-- 002_entitlements.sql
-- Client-scoped subscription entitlements (source of truth for step 5 execution gate).
-- Requires: 001_rls_foundation.sql (clients, client_members, user_client_ids()).
--
-- Run in Supabase SQL Editor or via psql against the project database.

BEGIN;

CREATE TABLE IF NOT EXISTS entitlements (
  client_id uuid PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
  plan text CHECK (plan IS NULL OR plan IN ('spark', 'starter', 'pro', 'business')),
  status text NOT NULL DEFAULT 'inactive'
    CHECK (status IN ('inactive', 'active', 'past_due', 'canceled')),
  stripe_customer_id text,
  stripe_subscription_id text,
  current_period_start timestamptz,
  current_period_end timestamptz,
  actions_limit int NOT NULL DEFAULT 0,
  agents_limit int NOT NULL DEFAULT 0,
  workflows_limit int NOT NULL DEFAULT 0,
  spend_cap_pence int NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_entitlements_subscription
  ON entitlements(stripe_subscription_id);

-- Bootstrap: one inactive row per existing client (idempotent).
INSERT INTO entitlements (
  client_id,
  status,
  plan,
  actions_limit,
  agents_limit,
  workflows_limit,
  spend_cap_pence
)
SELECT c.id, 'inactive', NULL, 0, 0, 0, 0
FROM clients c
ON CONFLICT (client_id) DO NOTHING;

ALTER TABLE entitlements ENABLE ROW LEVEL SECURITY;

CREATE POLICY entitlements_member_select ON entitlements
  FOR SELECT TO authenticated
  USING (client_id IN (SELECT public.user_client_ids()));

CREATE POLICY entitlements_service_role_all ON entitlements
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

COMMIT;

-- ─── DOWN (run manually to reverse) ──────────────────────────────────────────
/*
BEGIN;

DROP POLICY IF EXISTS entitlements_member_select ON entitlements;
DROP POLICY IF EXISTS entitlements_service_role_all ON entitlements;
ALTER TABLE entitlements DISABLE ROW LEVEL SECURITY;
DROP INDEX IF EXISTS idx_entitlements_subscription;
DROP TABLE IF EXISTS entitlements;

COMMIT;
*/
