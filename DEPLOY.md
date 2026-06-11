# Deploying LogiQ to Vercel

## Prerequisites

- [Vercel](https://vercel.com) account
- [Supabase](https://supabase.com) project
- [Stripe](https://stripe.com) account (test mode for staging)
- Google Cloud OAuth credentials for Gmail
- Anthropic API key

## 1. Supabase setup

1. Create a project at https://supabase.com/dashboard
2. Open **SQL Editor** and run the full contents of `backend/schema.sql`
3. Enable **Row Level Security** on all tables for production:
   - `user_profiles`: users can read/update own row (`auth.uid() = id`)
   - `user_integrations`: users can read own rows (`auth.uid() = user_id`)
   - `audit_log`, `usage_tracking`, agent data tables: scope by `user_id`
4. In **Authentication → URL Configuration**, set:
   - Site URL: `https://app.logiq.org.uk` (or your Vercel URL)
   - Redirect URLs: `https://app.logiq.org.uk/**`, `http://localhost:8000/**`
5. Copy **Project URL**, **anon key**, and **service role key** from Settings → API

## 2. Stripe setup

1. Create three recurring products in Stripe Dashboard:
   - Starter £49/mo → copy Price ID → `STRIPE_STARTER_PRICE_ID`
   - Pro £149/mo → `STRIPE_PRO_PRICE_ID`
   - Business £399/mo → `STRIPE_BUSINESS_PRICE_ID`
2. Copy **Secret key** → `STRIPE_SECRET_KEY`
3. Add webhook endpoint: `https://app.logiq.org.uk/api/billing/webhook`
   - Events: `checkout.session.completed`, `customer.subscription.deleted`
   - Copy signing secret → `STRIPE_WEBHOOK_SECRET`

## 3. Google OAuth (Gmail + Sheets)

1. Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client
2. Authorised redirect URI: `https://app.logiq.org.uk/api/auth/gmail/callback` (and localhost for dev)
3. Enable Gmail API and Google Sheets API
4. Set `GMAIL_SENDER_EMAIL` and `GMAIL_CREDENTIALS_JSON` in env

## 4. Deploy to Vercel

```bash
npm i -g vercel
cd "LogiQ Demo"
vercel
```

Or connect the GitHub repo in the Vercel dashboard.

### Environment variables (Vercel → Settings → Environment Variables)

Set all variables from `.env.example`. Minimum for production:

| Variable | Required |
|----------|----------|
| `ANTHROPIC_API_KEY` | Yes |
| `SUPABASE_URL` | Yes |
| `SUPABASE_ANON_KEY` | Yes |
| `SUPABASE_SERVICE_KEY` | Yes |
| `GMAIL_SENDER_EMAIL` | Yes (for email) |
| `GMAIL_CREDENTIALS_JSON` | Yes (for email) |
| `STRIPE_SECRET_KEY` | For billing |
| `STRIPE_WEBHOOK_SECRET` | For billing |
| `STRIPE_*_PRICE_ID` | For billing |
| `FRONTEND_URL` | Yes — `https://app.logiq.org.uk` |
| `OAUTH_REDIRECT_BASE` | Yes — same as FRONTEND_URL |

## 5. Custom domain (app.logiq.org.uk)

1. Vercel → Project → Settings → Domains → Add `app.logiq.org.uk`
2. Add DNS records at your registrar (CNAME to `cname.vercel-dns.com` or A records as shown)
3. Update Supabase auth redirect URLs and Google OAuth redirect URI to use the custom domain
4. Update Stripe webhook URL to `https://app.logiq.org.uk/api/billing/webhook`

## 6. Local development

```bash
cd backend
pip install -r requirements.txt
cp ../.env.example .env
# Fill in .env values
py -m uvicorn main:app --reload --port 8000
```

Open http://localhost:8000

On startup, the server prints `backend/schema.sql` to the terminal — run it in Supabase if you haven't already.

## 7. Verify deployment

- `GET /api/health` — returns `supabase_configured: true`
- Sign up via the login screen
- Connect Gmail from onboarding
- Deploy an agent via Build tab
- Check audit log on Dashboard
