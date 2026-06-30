-- 006_workflow_schedule.sql
-- Recurring workflow schedules. Apply manually in Supabase SQL Editor — committing
-- this file does NOT apply it to your live database.
--
-- schedule: JSON text, e.g. {"freq":"daily","time_utc":"09:00","weekday":1}
--   freq: once | hourly | daily | weekly
--   time_utc: HH:MM (UTC) for daily/weekly
--   weekday: 0=Mon … 6=Sun (weekly only)
-- next_run_at: when the cron runner should execute this workflow (UTC)

ALTER TABLE workflows
  ADD COLUMN IF NOT EXISTS schedule text DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS next_run_at timestamptz DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS last_run_at timestamptz DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_workflows_next_run
  ON workflows (next_run_at)
  WHERE status = 'active' AND schedule IS NOT NULL;
