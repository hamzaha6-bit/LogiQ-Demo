"""Robust .env loading with verbose logging and manual file fallback."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

logger = logging.getLogger("logiq.env")

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent
ENV_FILE = BACKEND_DIR / ".env"
ROOT_ENV_FILE = ROOT_DIR / ".env"

# Keys we log individually on startup
TRACKED_KEYS = (
    "ANTHROPIC_API_KEY",
    "GMAIL_SENDER_EMAIL",
    "GMAIL_CREDENTIALS_JSON",
    "SUPABASE_URL",
)

_file_cache: Optional[Dict[str, str]] = None


def _env_file_info(path: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "path": str(path.resolve()),
        "exists": path.exists(),
        "size_bytes": None,
        "readable": False,
    }
    if not path.exists():
        return info
    try:
        info["size_bytes"] = path.stat().st_size
        path.read_bytes()
        info["readable"] = True
    except OSError as exc:
        info["read_error"] = str(exc)
    return info


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse KEY=VALUE lines; later duplicates win (matches dotenv behaviour)."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read .env file %s: %s", path.resolve(), exc)
        return {}

    result: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def _apply_parsed_env(parsed: Dict[str, str], source: str) -> None:
    for key, value in parsed.items():
        if not value.strip():
            continue
        current = (os.getenv(key) or "").strip()
        if not current:
            os.environ[key] = value
            logger.info(
                "ENV [%s] %s loaded from %s (%d chars)",
                source,
                key,
                source,
                len(value),
            )
        elif current != value.strip():
            os.environ[key] = value
            logger.info(
                "ENV [%s] %s overridden from %s (%d chars, was %d chars)",
                source,
                key,
                source,
                len(value),
                len(current),
            )


def get_env_from_file(key: str, *, refresh: bool = False) -> str:
    """Return env var from os.environ, falling back to manual .env file parse."""
    global _file_cache
    value = (os.getenv(key) or "").strip()
    if value:
        return value

    if refresh or _file_cache is None:
        _file_cache = parse_env_file(ENV_FILE)
        if not _file_cache and ROOT_ENV_FILE.exists():
            _file_cache = parse_env_file(ROOT_ENV_FILE)

    file_value = (_file_cache.get(key) or "").strip()
    if file_value:
        os.environ[key] = _file_cache[key]
        logger.info(
            "ENV [fallback] %s loaded from manual .env parse (%d chars)",
            key,
            len(file_value),
        )
        return file_value
    return ""


def env_var_status(key: str) -> str:
    value = get_env_from_file(key)
    return "set" if value else "empty"


def log_env_var(key: str) -> None:
    """Log whether a tracked env var loaded (never logs the value)."""
    from_os = (os.getenv(key) or "").strip()
    file_parsed = parse_env_file(ENV_FILE).get(key, "")
    if not file_parsed.strip() and ROOT_ENV_FILE.exists():
        file_parsed = parse_env_file(ROOT_ENV_FILE).get(key, "")

    resolved = get_env_from_file(key)
    status = "SET" if resolved else "EMPTY"
    logger.info(
        "ENV CHECK %s: %s | os.environ=%d chars | file parse=%d chars | resolved=%d chars",
        key,
        status,
        len(from_os),
        len((file_parsed or "").strip()),
        len(resolved),
    )


def bootstrap_env() -> None:
    """Load .env with absolute paths, manual fallback, and verbose diagnostics."""
    backend_info = _env_file_info(ENV_FILE)
    root_info = _env_file_info(ROOT_ENV_FILE)

    logger.info("=== Environment bootstrap ===")
    logger.info(
        "Backend .env: path=%s exists=%s size_bytes=%s readable=%s",
        backend_info["path"],
        backend_info["exists"],
        backend_info["size_bytes"],
        backend_info["readable"],
    )
    if backend_info.get("read_error"):
        logger.error("Backend .env read error: %s", backend_info["read_error"])
    if backend_info.get("size_bytes") == 0:
        logger.warning(
            "%s is 0 bytes on disk — likely a OneDrive cloud placeholder. "
            "Open backend/.env, press Ctrl+S, or right-click → 'Always keep on this device'.",
            backend_info["path"],
        )

    if root_info["exists"]:
        logger.info(
            "Root .env: path=%s exists=%s size_bytes=%s readable=%s",
            root_info["path"],
            root_info["exists"],
            root_info["size_bytes"],
            root_info["readable"],
        )

    dotenv_ok = load_dotenv(ENV_FILE, override=True)
    logger.info("dotenv load_dotenv(backend/.env): %s", "OK" if dotenv_ok else "no variables loaded")

    if not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        root_ok = load_dotenv(ROOT_ENV_FILE, override=True)
        logger.info("dotenv load_dotenv(root/.env): %s", "OK" if root_ok else "skipped or empty")

    parsed_backend = parse_env_file(ENV_FILE)
    logger.info(
        "Manual parse backend/.env: %d keys found (file size %s bytes)",
        len(parsed_backend),
        backend_info["size_bytes"],
    )
    _apply_parsed_env(parsed_backend, "manual-backend")

    if ROOT_ENV_FILE.exists():
        parsed_root = parse_env_file(ROOT_ENV_FILE)
        if parsed_root:
            logger.info("Manual parse root/.env: %d keys found", len(parsed_root))
            _apply_parsed_env(parsed_root, "manual-root")

    global _file_cache
    _file_cache = parsed_backend or parse_env_file(ROOT_ENV_FILE)

    logger.info("--- Tracked environment variables ---")
    for key in TRACKED_KEYS:
        log_env_var(key)
    logger.info("=== Environment bootstrap complete ===")


def debug_env_status() -> Dict[str, Any]:
    """Return set/empty status for debug endpoint (never exposes values)."""
    backend_info = _env_file_info(ENV_FILE)
    return {
        "env_file": {
            "path": backend_info["path"],
            "exists": backend_info["exists"],
            "size_bytes": backend_info["size_bytes"],
            "readable": backend_info["readable"],
            "read_error": backend_info.get("read_error"),
        },
        "variables": {key: env_var_status(key) for key in TRACKED_KEYS},
        "gmail_configured": bool(get_env_from_file("GMAIL_SENDER_EMAIL") and get_env_from_file("GMAIL_CREDENTIALS_JSON")),
    }
