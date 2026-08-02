"""Shared pytest setup: force the offline SQLite backend before any
scripts/ module (db_core reads ALPHA_DB_BACKEND at import time)."""

from __future__ import annotations

import os

os.environ.setdefault("ALPHA_DB_BACKEND", "sqlite")
