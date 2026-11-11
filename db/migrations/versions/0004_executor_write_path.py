"""executor write path (Phase 3/5 code, DECISIONS D-27..D-29)

- ``change_log``: ``idempotency_key`` (unique per client; duplicate-execution guard),
  ``origin`` (proposal / emergency / pause_all / rollback / restore), ``error``; partial index
  on rows still ``applying`` for the reconciler.
- ``clients.execution_enabled``: master switch for platform writes, default false.
- ``write_requests``: API → engine queue for rollback / restore / pause-all.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11 18:18:01.667112+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('write_requests',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('dedupe_key', sa.Text(), nullable=False),
    sa.Column('target_id', sa.BigInteger(), nullable=True),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('requested_by', sa.Text(), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('status', sa.Text(), server_default='queued', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('rollback', 'restore', 'pause_all')", name=op.f('ck_write_requests_kind')),
    sa.CheckConstraint("status IN ('queued', 'running', 'success', 'failed', 'rejected')", name=op.f('ck_write_requests_status')),
    sa.ForeignKeyConstraint(['client_id'], ['clients.client_id'], name=op.f('fk_write_requests_client_id_clients'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_write_requests'))
    )
    op.create_index('ix_write_requests_client_status', 'write_requests', ['client_id', 'status'], unique=False)
    op.create_index('uq_write_requests_open', 'write_requests', ['client_id', 'dedupe_key'], unique=True, postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.add_column('change_log', sa.Column('idempotency_key', sa.Text(), nullable=True))
    op.add_column('change_log', sa.Column('origin', sa.Text(), server_default='proposal', nullable=False))
    op.add_column('change_log', sa.Column('error', sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f('ck_change_log_origin'),
        'change_log',
        "origin IN ('proposal', 'emergency', 'pause_all', 'rollback', 'restore')",
    )
    op.create_index('ix_change_log_applying', 'change_log', ['client_id', 'started_at'], unique=False, postgresql_where=sa.text("status = 'applying'"))
    op.create_unique_constraint(op.f('uq_change_log_client_id_idempotency_key'), 'change_log', ['client_id', 'idempotency_key'])
    op.add_column('clients', sa.Column('execution_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('clients', 'execution_enabled')
    op.drop_constraint(op.f('uq_change_log_client_id_idempotency_key'), 'change_log', type_='unique')
    op.drop_index('ix_change_log_applying', table_name='change_log', postgresql_where=sa.text("status = 'applying'"))
    op.drop_constraint(op.f('ck_change_log_origin'), 'change_log', type_='check')
    op.drop_column('change_log', 'error')
    op.drop_column('change_log', 'origin')
    op.drop_column('change_log', 'idempotency_key')
    op.drop_index('uq_write_requests_open', table_name='write_requests', postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.drop_index('ix_write_requests_client_status', table_name='write_requests')
    op.drop_table('write_requests')
