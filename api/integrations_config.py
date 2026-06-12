from http.server import BaseHTTPRequestHandler
import json

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({
            "gmail": False,
            "sheets": False,
            "gmail_configured": False,
            "gmail_authorised": False,
            "google_authorised": False,
            "sheets_configured": False,
            "sheets_available": False,
            "sheets_scope": False,
            "xero_configured": False,
            "hubspot_configured": False,
            "calendly_link": "",
        }).encode())
