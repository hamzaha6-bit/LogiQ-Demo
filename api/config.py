from http.server import BaseHTTPRequestHandler
import json
import os

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        response = {
            "supabase_url": os.environ.get("SUPABASE_URL", ""),
            "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
            "supabase_configured": bool(os.environ.get("SUPABASE_URL")),
            "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY"))
        }
        self.wfile.write(json.dumps(response).encode())
