-- 004_tos_acceptance.sql
-- Terms of Service acceptance tracking on user_profiles.
-- Existing users remain NULL until they accept on next login.

ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS tos_accepted_at timestamptz DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS tos_version_accepted text DEFAULT NULL;
