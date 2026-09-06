"""notifications

Revision ID: 0011_notifications
Revises: 0010_tender_raw
Create Date: 2026-09-06 12:05:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_notifications"
down_revision = "0010_tender_raw"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tender_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_notifications_tender_id"), ["tender_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_sent_at"), ["sent_at"], unique=False)
        # Der eindeutige Schluessel ist die Dedup-Garantie: dasselbe Ereignis
        # kann je Kanal nur einmal eingetragen werden, auch bei parallelen Laeufen.
        batch_op.create_index(
            "ix_notifications_channel_key", ["channel", "dedupe_key"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.drop_index("ix_notifications_channel_key")
        batch_op.drop_index(batch_op.f("ix_notifications_sent_at"))
        batch_op.drop_index(batch_op.f("ix_notifications_kind"))
        batch_op.drop_index(batch_op.f("ix_notifications_tender_id"))
    op.drop_table("notifications")
