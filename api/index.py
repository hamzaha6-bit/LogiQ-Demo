"""Vercel serverless entry — mounts the FastAPI app from backend/main.py."""
import os
import sys

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, BACKEND_DIR)

from main import app  # noqa: E402

try:
    from mangum import Mangum

    handler = Mangum(app, lifespan="off")
except ImportError:
    handler = app
