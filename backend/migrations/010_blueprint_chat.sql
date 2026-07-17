-- 010_blueprint_chat.sql
-- Blueprint conversation history + free-preview message counting.
-- FLAG: Apply manually in Supabase SQL Editor (after 009_workflow_soft_delete.sql).
-- Requires: 001_rls_foundation.sql (clients, user_client_ids()).

BEGIN;

CREATE TABLE IF NOT EXISTS blueprint_conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  agent_id text NOT NULL CHECK (agent_id IN ('aria', 'nova', 'finn', 'zara', 'cleo')),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);

CREATE TABLE IF NOT EXISTS blueprint_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES blueprint_conversations(id) ON DELETE CASCADE,
  client_id uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  agent_id text NOT NULL CHECK (agent_id IN ('aria', 'nova', 'finn', 'zara', 'cleo')),
  role text NOT NULL CHECK (role IN ('user', 'assistant')),
  content text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- One active conversation per user+agent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_blueprint_conversations_one_active
  ON blueprint_conversations (user_id, agent_id)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_blueprint_conversations_user_agent
  ON blueprint_conversations (user_id, agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_blueprint_messages_conversation
  ON blueprint_messages (conversation_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_blueprint_messages_user_role
  ON blueprint_messages (user_id, role, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_blueprint_messages_client
  ON blueprint_messages (client_id, created_at DESC);

ALTER TABLE blueprint_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE blueprint_messages ENABLE ROW LEVEL SECURITY;

-- Tenant membership + per-user privacy (Blueprint threads are personal).
CREATE POLICY blueprint_conversations_select ON blueprint_conversations
  FOR SELECT TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY blueprint_conversations_insert ON blueprint_conversations
  FOR INSERT TO authenticated
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY blueprint_conversations_update ON blueprint_conversations
  FOR UPDATE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  )
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY blueprint_conversations_delete ON blueprint_conversations
  FOR DELETE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY blueprint_conversations_service_role_all ON blueprint_conversations
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY blueprint_messages_select ON blueprint_messages
  FOR SELECT TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY blueprint_messages_insert ON blueprint_messages
  FOR INSERT TO authenticated
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY blueprint_messages_update ON blueprint_messages
  FOR UPDATE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  )
  WITH CHECK (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY blueprint_messages_delete ON blueprint_messages
  FOR DELETE TO authenticated
  USING (
    client_id IN (SELECT public.user_client_ids())
    AND user_id = auth.uid()
  );

CREATE POLICY blueprint_messages_service_role_all ON blueprint_messages
  FOR ALL TO service_role
  USING (true) WITH CHECK (true);

COMMIT;

-- ─── DOWN (run manually to reverse) ──────────────────────────────────────────
/*
BEGIN;

DROP POLICY IF EXISTS blueprint_messages_service_role_all ON blueprint_messages;
DROP POLICY IF EXISTS blueprint_messages_delete ON blueprint_messages;
DROP POLICY IF EXISTS blueprint_messages_update ON blueprint_messages;
DROP POLICY IF EXISTS blueprint_messages_insert ON blueprint_messages;
DROP POLICY IF EXISTS blueprint_messages_select ON blueprint_messages;
DROP POLICY IF EXISTS blueprint_conversations_service_role_all ON blueprint_conversations;
DROP POLICY IF EXISTS blueprint_conversations_delete ON blueprint_conversations;
DROP POLICY IF EXISTS blueprint_conversations_update ON blueprint_conversations;
DROP POLICY IF EXISTS blueprint_conversations_insert ON blueprint_conversations;
DROP POLICY IF EXISTS blueprint_conversations_select ON blueprint_conversations;

DROP INDEX IF EXISTS idx_blueprint_messages_client;
DROP INDEX IF EXISTS idx_blueprint_messages_user_role;
DROP INDEX IF EXISTS idx_blueprint_messages_conversation;
DROP INDEX IF EXISTS idx_blueprint_conversations_user_agent;
DROP INDEX IF EXISTS idx_blueprint_conversations_one_active;

DROP TABLE IF EXISTS blueprint_messages;
DROP TABLE IF EXISTS blueprint_conversations;

COMMIT;
*/
