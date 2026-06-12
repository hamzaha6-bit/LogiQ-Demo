import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

_IMPORT_ERROR = None
_IMPORT_TRACEBACK = ""
_mangum = None

try:
    from main import app  # noqa: E402
    from mangum import Mangum

    _mangum = Mangum(app, lifespan="off")
except Exception as exc:
    import traceback

    _IMPORT_ERROR = exc
    _IMPORT_TRACEBACK = traceback.format_exc()
    traceback.print_exc()


def _read_body(handler_self: BaseHTTPRequestHandler) -> str:
    length = int(handler_self.headers.get("Content-Length", 0))
    if length <= 0:
        return ""
    return handler_self.rfile.read(length).decode("utf-8", errors="replace")


def _build_event(handler_self: BaseHTTPRequestHandler, method: str, body: str) -> dict:
    parsed = urlparse(handler_self.path)
    headers = {k.lower(): v for k, v in handler_self.headers.items()}
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": parsed.path,
        "rawQueryString": parsed.query,
        "headers": headers,
        "requestContext": {"http": {"method": method, "path": parsed.path}},
        "body": body or None,
        "isBase64Encoded": False,
    }


def _write_mangum_response(handler_self: BaseHTTPRequestHandler, response: dict) -> None:
    status = int(response.get("statusCode", 500))
    handler_self.send_response(status)

    skip = {"content-length", "transfer-encoding", "connection"}
    for key, value in (response.get("headers") or {}).items():
        if key.lower() in skip:
            continue
        handler_self.send_header(key, value)

    handler_self.end_headers()

    body = response.get("body") or ""
    if response.get("isBase64Encoded"):
        import base64

        handler_self.wfile.write(base64.b64decode(body))
    elif isinstance(body, str):
        handler_self.wfile.write(body.encode("utf-8"))
    else:
        handler_self.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST", _read_body(self))

    def do_PUT(self):
        self._dispatch("PUT", _read_body(self))

    def do_PATCH(self):
        self._dispatch("PATCH", _read_body(self))

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str, body: str = "") -> None:
        if _mangum is None:
            self.send_response(500)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            payload = {
                "detail": "Failed to load backend/main.py",
                "error": str(_IMPORT_ERROR),
                "type": type(_IMPORT_ERROR).__name__ if _IMPORT_ERROR else "Unknown",
                "traceback": _IMPORT_TRACEBACK,
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        event = _build_event(self, method, body)
        response = _mangum(event, None)
        _write_mangum_response(self, response)
