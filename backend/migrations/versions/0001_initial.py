"""Initial schema: database.md §5.0→§5.14, §7, §19.2, §10 roles + grants.

Revision ID: 0001
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

from app.core.config import settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def _role(name: str, password: str | None) -> str:
    # database.md §10.1 roles. Login roles get a password from the environment (Vault in prod).
    login = f"LOGIN PASSWORD '{password}'" if password else "NOLOGIN"
    return f"""
    DO $$ BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{name}') THEN
        CREATE ROLE {name} {login};
      ELSE
        ALTER ROLE {name} {login};
      END IF;
    END $$;"""


def _run_sql(sql: str) -> None:
    """Run spec SQL verbatim: raw DBAPI cursor, no parameters, so '%' and ':' are never parsed as placeholders."""
    op.get_bind().connection.dbapi_connection.cursor().execute(sql)


def upgrade() -> None:
    op.execute(_role("am_app", settings.db_app_password))
    op.execute(_role("am_worker", settings.db_worker_password))
    op.execute(_role("am_readonly", None))
    op.execute(_role("am_audit_reader", None))
    _run_sql((SQL / "0001_schema.sql").read_text(encoding="utf-8"))
    _run_sql((SQL / "0001_grants.sql").read_text(encoding="utf-8"))
    # identity columns (audit_log.id, sync_changes.seq) draw from owned sequences
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO am_app, am_worker")


def downgrade() -> None:
    raise NotImplementedError("expand/contract only — no downgrades (database.md §13)")
