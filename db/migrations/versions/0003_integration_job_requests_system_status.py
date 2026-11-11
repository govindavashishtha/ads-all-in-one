"""integration: job_requests (manual trigger queue) + system_status (engine facts)

- ``job_requests``: POST /jobs/{name}/run inserts a queued row; the engine scheduler's
  ``job_requests`` poller claims and runs it (one open request per job).
- ``system_status``: latest pacing projection, heartbeat outcome and API operations per
  platform, written by the engine and read by GET /overview and GET /system.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('job_requests',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('job', sa.Text(), nullable=False),
    sa.Column('requested_by', sa.Text(), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('status', sa.Text(), server_default='queued', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'running', 'success', 'failed', 'rejected')", name=op.f('ck_job_requests_status')),
    sa.ForeignKeyConstraint(['client_id'], ['clients.client_id'], name=op.f('fk_job_requests_client_id_clients'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_requests'))
    )
    op.create_index('ix_job_requests_client_status', 'job_requests', ['client_id', 'status'], unique=False)
    op.create_index('uq_job_requests_open', 'job_requests', ['client_id', 'job'], unique=True, postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.create_table('system_status',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('client_id', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['clients.client_id'], name=op.f('fk_system_status_client_id_clients'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_system_status')),
    sa.UniqueConstraint('client_id', 'key', name=op.f('uq_system_status_client_id_key'))
    )


def downgrade() -> None:
    op.drop_table('system_status')
    op.drop_index('uq_job_requests_open', table_name='job_requests', postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.drop_index('ix_job_requests_client_status', table_name='job_requests')
    op.drop_table('job_requests')
