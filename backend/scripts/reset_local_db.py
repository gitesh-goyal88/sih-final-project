"""LOCAL/TEST ONLY: drop and recreate the public schema, re-run migrations and the demo seed.

Refuses to run unless AM_ENV is local or test (never against demo/staging/prod data).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import settings  # noqa: E402

if settings.env not in ("local", "test"):
    raise SystemExit(f"refusing to reset a '{settings.env}' database")

engine = create_engine(settings.database_url_owner_sync)
with engine.begin() as conn:
    conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
    conn.execute(text("CREATE SCHEMA public"))
    conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
root = Path(__file__).resolve().parents[1]
subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, cwd=root)
if "--no-seed" not in sys.argv:
    subprocess.run([sys.executable, "scripts/seed_demo.py"], check=True, cwd=root)
