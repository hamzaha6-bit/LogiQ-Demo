-- 005_postgrest_reload_schema.sql
-- Run AFTER 004_tos_acceptance.sql if ToS PATCH fails with PGRST204
-- ("Could not find the 'tos_accepted_at' column in the schema cache").
-- Does not modify data — only tells PostgREST to reload its schema cache.

NOTIFY pgrst, 'reload schema';
