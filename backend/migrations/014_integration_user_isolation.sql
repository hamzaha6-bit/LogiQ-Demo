-- 014_integration_user_isolation.sql
-- Per-user RLS on user_integrations and sheet_connections (JWT live path).
-- Co-members must not read/alter another member's Google token_data or sheet
-- connections. Same pattern as 012 (membership AND user_id = auth.uid()).
-- FLAG: Apply manually in Supabase SQL Editor (after 013_schema_health.sql).

BEGIN;

DROP POLICY IF EXISTS user_integrations_client_select ON user_integrations;
DROP POLICY IF EXISTS user_integrations_client_insert ON user_integrations;
DROP POLICY IF EXISTS user_integrations_client_update ON user_integrations;
DROP POLICY IF EXISTS user_integrations_client_delete ON user_integrations;

CREATE POLICY user_integrations_own_select ON user_integrations
  FOR SELECT TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY user_integrations_own_insert ON user_integrations
  FOR INSERT TO authenticated
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY user_integrations_own_update ON user_integrations
  FOR UPDATE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  )
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY user_integrations_own_delete ON user_integrations
  FOR DELETE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

DROP POLICY IF EXISTS sheet_connections_client_select ON sheet_connections;
DROP POLICY IF EXISTS sheet_connections_client_insert ON sheet_connections;
DROP POLICY IF EXISTS sheet_connections_client_update ON sheet_connections;
DROP POLICY IF EXISTS sheet_connections_client_delete ON sheet_connections;

CREATE POLICY sheet_connections_own_select ON sheet_connections
  FOR SELECT TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY sheet_connections_own_insert ON sheet_connections
  FOR INSERT TO authenticated
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY sheet_connections_own_update ON sheet_connections
  FOR UPDATE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  )
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY sheet_connections_own_delete ON sheet_connections
  FOR DELETE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

INSERT INTO schema_migrations (version) VALUES ('014_integration_user_isolation')
ON CONFLICT (version) DO NOTHING;

COMMIT;
