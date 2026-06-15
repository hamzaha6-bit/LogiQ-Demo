-- LogiQ Supabase schema — run in Supabase SQL Editor (enable RLS in production)

CREATE TABLE IF NOT EXISTS user_profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  name text,
  plan text DEFAULT 'starter',
  onboarding_complete boolean DEFAULT false,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_integrations (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  integration text NOT NULL,
  token_data jsonb,
  connected_at timestamptz DEFAULT now(),
  UNIQUE(user_id, integration)
);

CREATE TABLE IF NOT EXISTS audit_log (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid,
  agent text,
  action_type text,
  item_id text,
  recipient text,
  subject text,
  status text,
  metadata jsonb,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS usage_tracking (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid,
  date date DEFAULT CURRENT_DATE,
  api_calls int DEFAULT 0,
  emails_sent int DEFAULT 0,
  actions_taken int DEFAULT 0,
  UNIQUE(user_id, date)
);

-- Agent data tables (user-scoped via user_id column)
CREATE TABLE IF NOT EXISTS leads (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid,
  name text, company text, role text, industry text, email text,
  status text DEFAULT 'new', last_contacted timestamptz, emails_sent int DEFAULT 0,
  notes text, created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lead_emails (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid, lead_id uuid, subject text, body text, reasoning text,
  sent_at timestamptz, status text DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS invoices (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid, client text, amount numeric, currency text DEFAULT '£',
  due_date date, status text DEFAULT 'unpaid', emails_sent int DEFAULT 0,
  notes text, created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS invoice_emails (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid, invoice_id uuid, subject text, body text, reasoning text,
  sent_at timestamptz, status text DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS enquiries (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid, name text, company text, message text,
  status text DEFAULT 'new', response_draft text, created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reports (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid, title text, period text, raw_data text, content text,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid, title text, supplier text, due_date date,
  status text DEFAULT 'open', notes text, created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_actions (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id uuid, agent text, item_id text, reasoning text, action_type text,
  output text, status text DEFAULT 'queued', sent_at timestamptz,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_tracking_user_date ON usage_tracking(user_id, date);
CREATE INDEX IF NOT EXISTS idx_user_integrations_user ON user_integrations(user_id, integration);

-- ─── Welcome email (Supabase Auth Hook) ───────────────────────────────────────
-- In Supabase Dashboard → Authentication → Hooks → Before User Created:
--   Type: HTTP
--   URL:  https://logiqops.co.uk/api/auth/hook/user-created
--   Secret: copy to Vercel as SUPABASE_AUTH_HOOK_SECRET (full v1,whsec_… value)
-- Requires Vercel env: GMAIL_SENDER_EMAIL=hamza@logiq.org.uk, GMAIL_TOKEN_JSON=<OAuth token JSON>
