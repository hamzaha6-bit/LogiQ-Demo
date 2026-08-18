-- 016_stripe_webhook_events.sql
-- Idempotent Stripe event store + preserve purchased top-up packs across
-- subscription.updated (plan limits must not wipe actions_limit bonuses).
-- FLAG: Apply manually in Supabase SQL Editor (after 013_schema_health.sql;
-- independent of 014/015 — does not require those migrations).

BEGIN;

CREATE TABLE IF NOT EXISTS stripe_events (
  event_id text PRIMARY KEY,
  event_type text,
  processed_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE stripe_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS stripe_events_service_role_all ON stripe_events;
CREATE POLICY stripe_events_service_role_all ON stripe_events
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

ALTER TABLE entitlements
  ADD COLUMN IF NOT EXISTS purchased_topup_actions int NOT NULL DEFAULT 0;

COMMENT ON COLUMN entitlements.purchased_topup_actions IS
  'Extra actions bought via Stripe top-up packs. subscription.updated rebuilds actions_limit as plan limit + this column.';

INSERT INTO schema_migrations (version) VALUES ('016_stripe_webhook_events')
ON CONFLICT (version) DO NOTHING;

COMMIT;
