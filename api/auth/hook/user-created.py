"""Supabase Auth Hook endpoint — /api/auth/hook/user-created"""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from hook_handler import handle_user_created_hook, json_response


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        json_response(
            self,
            200,
            {"status": "ok", "hook": "user-created", "methods": ["GET", "POST"]},
        )

    def do_POST(self):
        handle_user_created_hook(self)
