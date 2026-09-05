"""tender raw

Revision ID: 0010_tender_raw
Revises: 0009_run_status
Create Date: 2026-09-05 22:55:00.000000
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0010_tender_raw"
down_revision = "0009_run_status"
branch_labels = None
depends_on = None

#: In einem Rutsch gelesene Datensaetze - haelt den Speicherbedarf der
#: Migration unabhaengig von der Zahl der Ausschreibungen klein.
BATCH = 500


def _move(connection: sa.Connection, *, into_table: bool) -> None:
    """``raw`` zwischen ``tenders.payload`` und ``tender_raw`` verschieben."""
    tenders = sa.table(
        "tenders",
        sa.column("id", sa.String),
        sa.column("payload", sa.JSON),
    )
    raw_table = sa.table(
        "tender_raw",
        sa.column("tender_id", sa.String),
        sa.column("raw", sa.JSON),
    )
    offset = 0
    while True:
        rows = connection.execute(
            sa.select(tenders.c.id, tenders.c.payload)
            .order_by(tenders.c.id)
            .limit(BATCH)
            .offset(offset)
        ).all()
        if not rows:
            return
        offset += len(rows)
        for tender_id, payload in rows:
            data = payload
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    continue
            if not isinstance(data, dict):
                continue

            if into_table:
                raw = data.pop("raw", None)
                if not raw:
                    continue
                connection.execute(raw_table.insert().values(tender_id=tender_id, raw=raw))
            else:
                stored = connection.execute(
                    sa.select(raw_table.c.raw).where(raw_table.c.tender_id == tender_id)
                ).scalar()
                if not stored:
                    continue
                data["raw"] = stored
            connection.execute(
                tenders.update().where(tenders.c.id == tender_id).values(payload=data)
            )


def upgrade() -> None:
    op.create_table(
        "tender_raw",
        sa.Column("tender_id", sa.String(length=255), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tender_id"),
    )
    # Bestehende Rohdaten umziehen, statt sie zu verlieren.
    _move(op.get_bind(), into_table=True)


def downgrade() -> None:
    _move(op.get_bind(), into_table=False)
    op.drop_table("tender_raw")
