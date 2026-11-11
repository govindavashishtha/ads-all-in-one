"""phase 2: notifications outbox + proposals.status 'escalated'

- ``notifications``: outbox for engine/notify (approval requests, reminders, digests).
- ``proposals.status`` gains ``escalated`` (rule outcome ``escalate`` waiting for LLM triage).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "'pending', 'blocked', 'awaiting_approval', 'approved', 'rejected', 'executing', 'executed', 'expired', 'failed'"
NEW_STATUSES = OLD_STATUSES + ", 'escalated'"


def upgrade() -> None:
    op.create_table('notifications',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('dedupe_key', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('subject', sa.Text(), nullable=False),
    sa.Column('body_text', sa.Text(), nullable=False),
    sa.Column('body_html', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('proposal_id', sa.BigInteger(), nullable=True),
    sa.Column('status', sa.Text(), server_default='queued', nullable=False),
    sa.Column('sent_channels', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('approval_request', 'approval_reminder', 'approval_expired', 'approval_blocked', 'digest', 'alert')", name=op.f('ck_notifications_kind')),
    sa.CheckConstraint("status IN ('queued', 'sent', 'failed')", name=op.f('ck_notifications_status')),
    sa.ForeignKeyConstraint(['client_id'], ['clients.client_id'], name=op.f('fk_notifications_client_id_clients'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['proposal_id'], ['proposals.id'], name=op.f('fk_notifications_proposal_id_proposals')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
    sa.UniqueConstraint('client_id', 'dedupe_key', name=op.f('uq_notifications_client_id_dedupe_key'))
    )
    op.create_index('ix_notifications_client_status', 'notifications', ['client_id', 'status'], unique=False)
    op.drop_constraint(op.f('ck_proposals_status'), 'proposals', type_='check')
    op.create_check_constraint(op.f('ck_proposals_status'), 'proposals', f"status IN ({NEW_STATUSES})")


def downgrade() -> None:
    op.execute("UPDATE proposals SET status = 'pending' WHERE status = 'escalated'")
    op.drop_constraint(op.f('ck_proposals_status'), 'proposals', type_='check')
    op.create_check_constraint(op.f('ck_proposals_status'), 'proposals', f"status IN ({OLD_STATUSES})")
    op.drop_index('ix_notifications_client_status', table_name='notifications')
    op.drop_table('notifications')
