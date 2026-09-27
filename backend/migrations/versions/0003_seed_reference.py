"""Reference data from database.md §11.

Revision ID: 0003
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def _run_sql(sql: str) -> None:
    """Run spec SQL verbatim: raw DBAPI cursor, no parameters, so '%' and ':' are never parsed as placeholders."""
    op.get_bind().connection.dbapi_connection.cursor().execute(sql)


def upgrade() -> None:
    _run_sql((SQL / "0003_seed_reference.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("expand/contract only — no downgrades (database.md §13)")
