# LogiQ - Your Digital Employee

A full-stack AI agent platform demo. Visitors open a URL and use the complete LogiQ workforce — Blueprint AI, five default agents, custom agent deployment, and action queues — with no client-side API keys required.

## Architecture

| Layer | Tech |
|-------|------|
| Frontend | Single `index.html` — vanilla HTML/CSS/JS |
| Backend | Python FastAPI (`/backend`) |
| AI | Claude Sonnet 4.5 via Anthropic API (server-side) |
| Email | Gmail API via service account |
| Data | In-memory by default; optional Supabase for persistence |

## Quick start (local)

### 1. Install backend dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp ../.env.example ../.env
# Edit .env and set at minimum:
#   ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Run the server

```bash
cd backend
uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000** — the API serves `index.html` and all `/api/*` routes from one process.

The sidebar shows a green **API connected** dot when `/api/health` returns `{"status":"ok"}`. No keys needed in the browser.

## API routes

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/chat` | Claude chat proxy `{messages, system, max_tokens}` → `{content}` |
| `POST` | `/api/agent/run` | Agent pipeline (SSE) `{agent, items, settings}` |
| `POST` | `/api/send/gmail` | Send email `{to, subject, body}` |
| `GET` | `/api/auth/{integration}/connect` | OAuth URL (gmail, hubspot, xero) |
| `GET` | `/api/auth/{integration}/callback` | OAuth callback scaffold |

Rate limit: **30 requests/minute per IP** on all `/api/*` routes except health.

## Agent pipeline (SSE)

`POST /api/agent/run` streams Server-Sent Events:

```
event: progress
data: {"current": 1, "total": 10}

event: result
data: {"item_id": "...", "reasoning": "...", "action": "email", "subject": "...", "body": "..."}

event: done
data: {"total": 10, "queued": 7}
```

The frontend updates the action queue in real time as each `result` event arrives.

## Integrations

| Integration | Agent | Env vars | UI |
|-------------|-------|----------|-----|
| Gmail send | All | `GMAIL_SENDER_EMAIL`, `GMAIL_CREDENTIALS_JSON` | Send via Gmail on approve |
| Google Sheets | Aria | `GOOGLE_SHEETS_CREDENTIALS_JSON` | Connect Google Sheet URL |
| Calendly | Nova | `CALENDLY_LINK` | Booking link in settings |
| Xero | Finn | `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET`, `XERO_TENANT_ID`, `XERO_REFRESH_TOKEN` | Sync from Xero button |
| HubSpot | Aria | `HUBSPOT_API_KEY` | Auto-sync on approve (warm/converted leads) |

Missing credentials fail gracefully with toast messages — the app never crashes.

### Gmail setup

1. Create OAuth2 **Desktop** or **Web** credentials in Google Cloud Console with Gmail API enabled.
2. Add redirect URI: `http://localhost:8000/api/auth/gmail/callback`
3. Paste the downloaded client secrets JSON into `GMAIL_CREDENTIALS_JSON` in `backend/.env`
4. Set `GMAIL_SENDER_EMAIL` to the Gmail account you will authorise.
5. Visit **http://localhost:8000/api/auth/gmail/connect** once to authorise — token saves to `backend/token.json`.

If not authorised, send returns **401** with `"Gmail not authorised — visit /api/auth/gmail/connect"`.

## Optional Supabase

The bottom banner (hidden by default) accepts Supabase URL + anon key for client-side persistence. Run the schema SQL logged to the browser console on first connect.

For OAuth token storage (scaffold), set `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` on the backend and create an `oauth_tokens` table.

## Deploy to Vercel

1. Push the repo to GitHub.
2. Import the project in Vercel.
3. Add environment variables from `.env.example` (at minimum `ANTHROPIC_API_KEY`).
4. Deploy — `vercel.json` routes `/api/*` to the Python backend and serves `index.html` for all other paths.

## Demo mode

If the backend is unreachable (e.g. opening `index.html` directly without a server), the sidebar shows **Demo mode** with an amber dot. The UI still works with in-memory data; AI features require either the backend or a developer fallback key set via:

```js
KEYS.anthropic = 'sk-ant-...'  // browser console only — local dev
```

## Project structure

```
LogiQ Demo/
├── index.html          # Full frontend (single file)
├── backend/
│   ├── main.py         # FastAPI app
│   ├── gmail_service.py
│   ├── rate_limit.py
│   └── requirements.txt
├── vercel.json
├── .env.example
└── README.md
```
