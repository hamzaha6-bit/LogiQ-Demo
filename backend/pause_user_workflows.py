"""One-off: pause all workflows for a user by email."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "api_lib"))

from env_loader import bootstrap_env
from supabase_rest import pause_workflows_for_user, user_id_from_email

EMAIL = "hamzaarif01@outlook.com"


def main() -> int:
    bootstrap_env()
    user_id = user_id_from_email(EMAIL)
    if not user_id:
        print(f"User not found: {EMAIL}")
        return 1
    paused_count, err = pause_workflows_for_user(user_id, active_only=False)
    if err:
        print(f"Failed: {err}")
        return 1
    print(f"Paused {paused_count} workflow(s) for {EMAIL} ({user_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
