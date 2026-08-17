-- 015_managed_output_tabs.sql
-- Persist GS-10 tab titles owned by a sheet connection so emit deletes only
-- tabs this workflow created, not any workbook tab matching "Picklist N".
-- FLAG: Apply manually in Supabase SQL Editor (after 013_schema_health.sql;
-- independent of 014 — adds a column only).

BEGIN;

ALTER TABLE sheet_connections
  ADD COLUMN IF NOT EXISTS managed_output_titles jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN sheet_connections.managed_output_titles IS
  'Titles GS-10 last wrote for this connection (Picklist N / Exceptions). Next emit deletes only these plus this run''s planned titles — never a name-pattern sweep of the workbook.';

INSERT INTO schema_migrations (version) VALUES ('015_managed_output_tabs')
ON CONFLICT (version) DO NOTHING;

COMMIT;
