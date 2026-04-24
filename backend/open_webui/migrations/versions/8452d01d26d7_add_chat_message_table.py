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

log = logging.getLogger(__name__)

revision: str = '8452d01d26d7'
down_revision: Union[str, None] = '374d2f66af06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH_SIZE = 5000


def _flush_batch(conn, table, batch):
    """
    Insert a batch of messages in its own transaction, falling back to
    row-by-row on error. Each batch commits independently so progress is
    durable across pod restarts.
    """
    tx = conn.begin()
    try:
        conn.execute(sa.insert(table), batch)
        tx.commit()
        return len(batch), 0
    except Exception:
        tx.rollback()
        inserted = 0
        failed = 0
        for msg in batch:
            row_tx = conn.begin()
            try:
                conn.execute(sa.insert(table).values(**msg))
                row_tx.commit()
                inserted += 1
            except Exception as e:
                row_tx.rollback()
                failed += 1
                log.warning(f'Failed to insert message {msg["id"]}: {e}')
        return inserted, failed


def upgrade() -> None:
    # Step 1: Create table
    op.create_table(
        'chat_message',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('chat_id', sa.Text(), nullable=False, index=True),
        sa.Column('user_id', sa.Text(), index=True),
        sa.Column('role', sa.Text(), nullable=False),
        sa.Column('parent_id', sa.Text(), nullable=True),
        sa.Column('content', sa.JSON(), nullable=True),
        sa.Column('output', sa.JSON(), nullable=True),
        sa.Column('model_id', sa.Text(), nullable=True, index=True),
        sa.Column('files', sa.JSON(), nullable=True),
        sa.Column('sources', sa.JSON(), nullable=True),
        sa.Column('embeds', sa.JSON(), nullable=True),
        sa.Column('done', sa.Boolean(), default=True),
        sa.Column('status_history', sa.JSON(), nullable=True),
        sa.Column('error', sa.JSON(), nullable=True),
        sa.Column('usage', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.BigInteger(), index=True),
        sa.Column('updated_at', sa.BigInteger()),
        sa.ForeignKeyConstraint(['chat_id'], ['chat.id'], ondelete='CASCADE'),
    )

    # Create composite indexes
    op.create_index('chat_message_chat_parent_idx', 'chat_message', ['chat_id', 'parent_id'])
    op.create_index('chat_message_model_created_idx', 'chat_message', ['model_id', 'created_at'])
    op.create_index('chat_message_user_created_idx', 'chat_message', ['user_id', 'created_at'])

    # Step 2: Backfill from existing chats.
    #
    # Run in autocommit_block so batches commit as we go. Without this the
    # whole backfill is one transaction: WAL/undo/locks grow unbounded, pod
    # OOMs mid-migration, rollback discards all progress, restart repeats.
    with op.get_context().autocommit_block():
        _backfill(op.get_bind())


def _backfill(conn) -> None:
    chat_table = sa.table(
        'chat',
        sa.column('id', sa.Text()),
        sa.column('user_id', sa.Text()),
        sa.column('chat', sa.JSON()),
    )

    chat_message_table = sa.table(
        'chat_message',
        sa.column('id', sa.Text()),
        sa.column('chat_id', sa.Text()),
        sa.column('user_id', sa.Text()),
        sa.column('role', sa.Text()),
        sa.column('parent_id', sa.Text()),
        sa.column('content', sa.JSON()),
        sa.column('output', sa.JSON()),
        sa.column('model_id', sa.Text()),
        sa.column('files', sa.JSON()),
        sa.column('sources', sa.JSON()),
        sa.column('embeds', sa.JSON()),
        sa.column('done', sa.Boolean()),
        sa.column('status_history', sa.JSON()),
        sa.column('error', sa.JSON()),
        sa.column('usage', sa.JSON()),
        sa.column('created_at', sa.BigInteger()),
        sa.column('updated_at', sa.BigInteger()),
    )

    # Skip chats already backfilled (restart-safe).
    processed_chat_ids = {
        r[0] for r in conn.execute(
            sa.text('SELECT DISTINCT chat_id FROM chat_message')
        )
    }
    if processed_chat_ids:
        log.info(f'Resuming: {len(processed_chat_ids)} chats already backfilled, skipping.')

    # Stream rows instead of loading all into memory:
    # - yield_per: fetches rows in chunks via cursor.fetchmany() (all backends)
    # - stream_results: enables server-side cursors on PostgreSQL (no-op on SQLite)
    result = conn.execute(
        sa.select(chat_table.c.id, chat_table.c.user_id, chat_table.c.chat)
        .where(~chat_table.c.user_id.like('shared-%'))
        .execution_options(yield_per=100, stream_results=True)
    )

    now = int(time.time())
    messages_batch = []
    total_inserted = 0
    total_failed = 0

    for chat_row in result:
        chat_id = chat_row[0]
        user_id = chat_row[1]
        chat_data = chat_row[2]

        if chat_id in processed_chat_ids:
            continue

        if not chat_data:
            continue

        # Handle both string and dict chat data
        if isinstance(chat_data, str):
            try:
                chat_data = json.loads(chat_data)
            except Exception:
                continue

        history = chat_data.get('history', {})
        if not isinstance(history, dict):
            continue

        messages = history.get('messages', {})
        if not isinstance(messages, dict):
            continue

        for message_id, message in messages.items():
            if not isinstance(message, dict):
                continue

            role = message.get('role')
            if not role:
                continue

            timestamp = message.get('timestamp', now)

            try:
                timestamp = int(float(timestamp))
            except Exception as e:
                timestamp = now

            # Normalize timestamp: convert ms to seconds, validate range
            if timestamp > 10_000_000_000:
                timestamp = timestamp // 1000
            # Must be after 2020 and not too far in the future
            if timestamp < 1577836800 or timestamp > now + 86400:
                timestamp = now

            messages_batch.append(
                {
                    'id': f'{chat_id}-{message_id}',
                    'chat_id': chat_id,
                    'user_id': user_id,
                    'role': role,
                    'parent_id': message.get('parentId'),
                    'content': message.get('content'),
                    'output': message.get('output'),
                    'model_id': message.get('model'),
                    'files': message.get('files'),
                    'sources': message.get('sources'),
                    'embeds': message.get('embeds'),
                    'done': message.get('done', True),
                    'status_history': message.get('statusHistory'),
                    'error': message.get('error'),
                    'usage': message.get('usage'),
                    'created_at': timestamp,
                    'updated_at': timestamp,
                }
            )

            # Flush batch when full
            if len(messages_batch) >= BATCH_SIZE:
                inserted, failed = _flush_batch(conn, chat_message_table, messages_batch)
                total_inserted += inserted
                total_failed += failed
                if total_inserted % 50000 < BATCH_SIZE:
                    log.info(f'Migration progress: {total_inserted} messages inserted...')
                messages_batch.clear()

    # Flush remaining messages
    if messages_batch:
        inserted, failed = _flush_batch(conn, chat_message_table, messages_batch)
        total_inserted += inserted
        total_failed += failed

    log.info(f'Backfilled {total_inserted} messages into chat_message table ({total_failed} failed)')


def downgrade() -> None:
    op.drop_index('chat_message_user_created_idx', table_name='chat_message')
    op.drop_index('chat_message_model_created_idx', table_name='chat_message')
    op.drop_index('chat_message_chat_parent_idx', table_name='chat_message')
    op.drop_table('chat_message')
