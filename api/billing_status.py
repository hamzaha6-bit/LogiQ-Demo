from http.server import BaseHTTPRequestHandler
import json

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({
            "plan": "starter",
            "plan_name": "Starter",
            "usage": {
                "api_calls": 0,
                "emails_sent": 0,
                "api_calls_today": 0,
                "emails_sent_today": 0,
                "actions_this_month": 0,
            },
            "limits": {},
            "percentages": {"api_calls": 0, "emails": 0, "actions": 0},
            "stripe_configured": False,
        }).encode())
