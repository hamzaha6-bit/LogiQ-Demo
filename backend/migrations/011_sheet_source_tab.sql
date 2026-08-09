-- 011_sheet_source_tab.sql
-- Distinguish schema-locked source tab from unlocked output tabs (GS-08/GS-09).
-- FLAG: Apply manually in Supabase SQL Editor (after 010_blueprint_chat.sql).

BEGIN;

ALTER TABLE sheet_connections
  ADD COLUMN IF NOT EXISTS source_sheet_name text;

COMMENT ON COLUMN sheet_connections.source_sheet_name IS
  'Schema-locked source tab title (GS-05). Output tabs use explicit sheet_name on GS-08/GS-09 and are not stored here.';

COMMIT;
