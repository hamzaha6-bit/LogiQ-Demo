import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

_IMPORT_ERROR = None
_IMPORT_TRACEBACK = ""

try:
    from main import app  # noqa: E402
except Exception as exc:
    import traceback

    _IMPORT_ERROR = exc
    _IMPORT_TRACEBACK = traceback.format_exc()
    traceback.print_exc()

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="LogiQ API (import fallback)")

    @app.get("/api/ping")
    async def ping():
        return {"status": "ok", "main_loaded": False}

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def import_error(path: str = ""):
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Failed to load backend/main.py",
                "error": str(_IMPORT_ERROR),
                "type": type(_IMPORT_ERROR).__name__,
                "traceback": _IMPORT_TRACEBACK,
            },
        )

try:
    from mangum import Mangum

    handler = Mangum(app, lifespan="off")
except ImportError:
    handler = app
