"""run status

Revision ID: 0009_run_status
Revises: 0008_decisions
Create Date: 2026-09-05 22:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_run_status"
down_revision = "0008_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Bestehende Laeufe sind abgeschlossen (der Prozess, der sie schrieb, ist
    # laengst beendet) - sie bekommen "finished" statt "running".
    with op.batch_alter_table("ingest_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="running",
            )
        )
    op.execute("UPDATE ingest_runs SET status = 'finished' WHERE finished_at IS NOT NULL")
    op.execute("UPDATE ingest_runs SET status = 'aborted' WHERE finished_at IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("ingest_runs", schema=None) as batch_op:
        batch_op.drop_column("status")
