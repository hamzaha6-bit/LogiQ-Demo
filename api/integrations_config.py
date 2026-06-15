from http.server import BaseHTTPRequestHandler
import json
import os

from google_oauth import is_oauth_configured
from http_auth import resolve_user_id


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        user_id = resolve_user_id(self)
        oauth_ok = is_oauth_configured()
        sheets_scope = False
        calendar_scope = False
        gmail_authorised = False
        email = ""

        if user_id and oauth_ok:
            from google_oauth import check_gmail_health

            health = check_gmail_health(user_id)
            gmail_authorised = health.get("healthy", False)
            sheets_scope = health.get("sheets_scope", False)
            calendar_scope = health.get("calendar_scope", False)
            email = health.get("email", "")

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "gmail": oauth_ok and gmail_authorised,
                    "gmail_configured": oauth_ok,
                    "gmail_authorised": gmail_authorised,
                    "google_authorised": gmail_authorised,
                    "sheets_configured": oauth_ok,
                    "sheets_available": oauth_ok and sheets_scope,
                    "sheets_scope": sheets_scope,
                    "calendar_scope": calendar_scope,
                    "calendar_configured": oauth_ok,
                    "sender_email": email,
                    "xero_configured": bool((os.environ.get("XERO_CLIENT_ID") or "").strip()),
                    "hubspot_configured": bool((os.environ.get("HUBSPOT_API_KEY") or "").strip()),
                    "calendly_link": (os.environ.get("CALENDLY_LINK") or "").strip(),
                }
            ).encode()
        )
