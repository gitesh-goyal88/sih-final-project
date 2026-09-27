"""Maintenance entry points for the bulk queue (database.md §12, §16.3, §19.2).

am_worker has no DDL (database.md §10.1), but `ensure_partitions` must create partitions and
`refresh_materialized_views` must refresh MVs (owner-only in PostgreSQL). Both are exposed as
narrow SECURITY DEFINER functions owned by the migrating role and granted to am_worker only.

Revision ID: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _run_sql(sql: str) -> None:
    """Run spec SQL verbatim: raw DBAPI cursor, no parameters, so '%' and ':' are never parsed as placeholders."""
    op.get_bind().connection.dbapi_connection.cursor().execute(sql)


def upgrade() -> None:
    _run_sql(
        """
CREATE OR REPLACE FUNCTION am_refresh_materialized_views() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  REFRESH MATERIALIZED VIEW CONCURRENTLY mv_district_daily_kpis;
  REFRESH MATERIALIZED VIEW CONCURRENTLY mv_facility_response_stats;
END $$;

-- weekly sync_changes partitions (ISO weeks, Monday start) and monthly audit_log partitions
CREATE OR REPLACE FUNCTION am_ensure_partitions(p_weeks_ahead int DEFAULT 4, p_months_ahead int DEFAULT 3)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  d date; n int := 0; name text;
BEGIN
  FOR i IN 0..p_weeks_ahead LOOP
    d := date_trunc('week', current_date + (i * 7))::date;
    name := format('sync_changes_%sw%s', to_char(d, 'IYYY'), to_char(d, 'IW'));
    IF to_regclass(name) IS NULL THEN
      EXECUTE format('CREATE TABLE %I PARTITION OF sync_changes FOR VALUES FROM (%L) TO (%L)', name, d, d + 7);
      n := n + 1;
    END IF;
  END LOOP;
  FOR i IN 0..p_months_ahead LOOP
    d := (date_trunc('month', current_date) + make_interval(months => i))::date;
    name := format('audit_log_%s', to_char(d, 'YYYY_MM'));
    IF to_regclass(name) IS NULL THEN
      EXECUTE format('CREATE TABLE %I PARTITION OF audit_log FOR VALUES FROM (%L) TO (%L)',
                     name, d, (d + interval '1 month')::date);
      n := n + 1;
    END IF;
  END LOOP;
  RETURN n;
END $$;

REVOKE ALL ON FUNCTION am_refresh_materialized_views() FROM PUBLIC;
REVOKE ALL ON FUNCTION am_ensure_partitions(int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION am_refresh_materialized_views() TO am_worker;
GRANT EXECUTE ON FUNCTION am_ensure_partitions(int, int) TO am_worker;
"""
    )


def downgrade() -> None:
    raise NotImplementedError("expand/contract only — no downgrades (database.md §13)")
