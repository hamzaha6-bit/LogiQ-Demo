-- 013_schema_health.sql
-- Record applied migration versions for /api/health probes.
-- FLAG: Apply manually in Supabase SQL Editor (after 012_workflow_isolation.sql).

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES
  ('001_rls_foundation'),
  ('002_entitlements'),
  ('003_client_usage'),
  ('004_tos_acceptance'),
  ('005_postgrest_reload_schema'),
  ('006_workflow_schedule'),
  ('007_workflow_runs'),
  ('008_agent_limits'),
  ('009_workflow_soft_delete'),
  ('010_blueprint_chat'),
  ('011_sheet_source_tab'),
  ('012_workflow_isolation'),
  ('013_schema_health')
ON CONFLICT (version) DO NOTHING;

ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS schema_migrations_service_role_all ON schema_migrations;
CREATE POLICY schema_migrations_service_role_all ON schema_migrations
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

COMMIT;
