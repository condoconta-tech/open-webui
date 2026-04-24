"""Add chat_message table

Revision ID: 8452d01d26d7
Revises: 374d2f66af06
Create Date: 2026-02-01 04:00:00.000000

"""

import time
import json
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

log = logging.getLogger(__name__)

revision: str = "8452d01d26d7"
down_revision: Union[str, None] = "374d2f66af06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BATCH_CHATS = 50
MAX_ROWS_PER_INSERT = 500


def upgrade() -> None:
    # Step 1: Create table (no indexes yet — create after backfill for speed)
    op.create_table(
        "chat_message",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text()),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("content", sa.JSON(), nullable=True),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("files", sa.JSON(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("embeds", sa.JSON(), nullable=True),
        sa.Column("done", sa.Boolean(), default=True),
        sa.Column("status_history", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger()),
        sa.Column("updated_at", sa.BigInteger()),
        sa.ForeignKeyConstraint(["chat_id"], ["chat.id"], ondelete="CASCADE"),
    )

    # Step 2: Backfill from existing chats (paginated, bulk insert)
    conn = op.get_bind()
    dialect = conn.dialect.name

    chat_table = sa.table(
        "chat",
        sa.column("id", sa.Text()),
        sa.column("user_id", sa.Text()),
        sa.column("chat", sa.JSON()),
    )

    chat_message_table = sa.table(
        "chat_message",
        sa.column("id", sa.Text()),
        sa.column("chat_id", sa.Text()),
        sa.column("user_id", sa.Text()),
        sa.column("role", sa.Text()),
        sa.column("parent_id", sa.Text()),
        sa.column("content", sa.JSON()),
        sa.column("output", sa.JSON()),
        sa.column("model_id", sa.Text()),
        sa.column("files", sa.JSON()),
        sa.column("sources", sa.JSON()),
        sa.column("embeds", sa.JSON()),
        sa.column("done", sa.Boolean()),
        sa.column("status_history", sa.JSON()),
        sa.column("error", sa.JSON()),
        sa.column("usage", sa.JSON()),
        sa.column("created_at", sa.BigInteger()),
        sa.column("updated_at", sa.BigInteger()),
    )

    now = int(time.time())
    messages_inserted = 0
    messages_failed = 0

    def flush(rows):
        nonlocal messages_inserted, messages_failed
        if not rows:
            return
        try:
            if dialect == "postgresql":
                stmt = pg_insert(chat_message_table).values(rows).on_conflict_do_nothing(
                    index_elements=["id"]
                )
            else:
                stmt = sa.insert(chat_message_table).values(rows).prefix_with("OR IGNORE")
            conn.execute(stmt)
            messages_inserted += len(rows)
        except Exception as e:
            log.warning(f"Bulk insert failed ({len(rows)} rows), falling back per-row: {e}")
            for row in rows:
                sp = conn.begin_nested()
                try:
                    conn.execute(sa.insert(chat_message_table).values(**row))
                    sp.commit()
                    messages_inserted += 1
                except Exception as ee:
                    sp.rollback()
                    messages_failed += 1
                    log.warning(f"Failed to insert message {row.get('id')}: {ee}")

    offset = 0
    pending = []

    while True:
        chats = conn.execute(
            sa.select(chat_table.c.id, chat_table.c.user_id, chat_table.c.chat)
            .where(~chat_table.c.user_id.like("shared-%"))
            .order_by(chat_table.c.id)
            .limit(BATCH_CHATS)
            .offset(offset)
        ).fetchall()

        if not chats:
            break

        for chat_row in chats:
            chat_id = chat_row[0]
            user_id = chat_row[1]
            chat_data = chat_row[2]

            if not chat_data:
                continue

            if isinstance(chat_data, str):
                try:
                    chat_data = json.loads(chat_data)
                except Exception:
                    continue

            history = chat_data.get("history", {})
            messages = history.get("messages", {})

            for message_id, message in messages.items():
                if not isinstance(message, dict):
                    continue

                role = message.get("role")
                if not role:
                    continue

                timestamp = message.get("timestamp", now)
                try:
                    timestamp = int(float(timestamp))
                except Exception:
                    timestamp = now

                if timestamp > 10_000_000_000:
                    timestamp = timestamp // 1000
                if timestamp < 1577836800 or timestamp > now + 86400:
                    timestamp = now

                pending.append(
                    {
                        "id": f"{chat_id}-{message_id}",
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "role": role,
                        "parent_id": message.get("parentId"),
                        "content": message.get("content"),
                        "output": message.get("output"),
                        "model_id": message.get("model"),
                        "files": message.get("files"),
                        "sources": message.get("sources"),
                        "embeds": message.get("embeds"),
                        "done": message.get("done", True),
                        "status_history": message.get("statusHistory"),
                        "error": message.get("error"),
                        "usage": None,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    }
                )

                if len(pending) >= MAX_ROWS_PER_INSERT:
                    flush(pending)
                    pending = []

        offset += BATCH_CHATS

    flush(pending)
    pending = []

    # Step 3: Create indexes AFTER backfill (much faster than per-row updates)
    op.create_index("ix_chat_message_chat_id", "chat_message", ["chat_id"])
    op.create_index("ix_chat_message_user_id", "chat_message", ["user_id"])
    op.create_index("ix_chat_message_model_id", "chat_message", ["model_id"])
    op.create_index("ix_chat_message_created_at", "chat_message", ["created_at"])
    op.create_index(
        "chat_message_chat_parent_idx", "chat_message", ["chat_id", "parent_id"]
    )
    op.create_index(
        "chat_message_model_created_idx", "chat_message", ["model_id", "created_at"]
    )
    op.create_index(
        "chat_message_user_created_idx", "chat_message", ["user_id", "created_at"]
    )

    log.info(
        f"Backfilled {messages_inserted} messages into chat_message table ({messages_failed} failed)"
    )


def downgrade() -> None:
    op.drop_index("chat_message_user_created_idx", table_name="chat_message")
    op.drop_index("chat_message_model_created_idx", table_name="chat_message")
    op.drop_index("chat_message_chat_parent_idx", table_name="chat_message")
    op.drop_index("ix_chat_message_created_at", table_name="chat_message")
    op.drop_index("ix_chat_message_model_id", table_name="chat_message")
    op.drop_index("ix_chat_message_user_id", table_name="chat_message")
    op.drop_index("ix_chat_message_chat_id", table_name="chat_message")
    op.drop_table("chat_message")
