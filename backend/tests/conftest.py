"""Shared test env — crypto and cron secrets required at api_lib import time."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("CRON_SECRET", "test-cron-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
